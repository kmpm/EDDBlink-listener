#!/usr/bin/env python3.6

from __future__ import generators
import json
import time
import zlib
import zmq
import threading
import trade
import tradedb
import tradeenv
import transfers
import datetime
import sqlite3
import csv
import codecs
import plugins.eddblink_plug
import sys

from urllib import request
from calendar import timegm
from pathlib import Path
from collections import defaultdict, namedtuple, deque, OrderedDict
from distutils.version import LooseVersion

# Copyright (C) Oliver 'kfsone' Smith <oliver@kfs.org> 2015
#
# Conditional permission to copy, modify, refactor or use this
# code is granted so long as attribution to the original author
# is included.
class MarketPrice(namedtuple('MarketPrice', [
        'system',
        'station',
        'commodities',
        'timestamp',
        'uploader',
        'software',
        'version',
        ])):
    pass

class Listener(object):
    """
    Provides an object that will listen to the Elite Dangerous Data Network
    firehose and capture messages for later consumption.

    Rather than individual upates, prices are captured across a window of
    between minBatchTime and maxBatchTime. When a new update is received,
    Rather than returning individual messages, messages are captured across
    a window of potentially several seconds and returned to the caller in
    batches.

    Attributes:
        zmqContext          Context this object is associated with,
        minBatchTime        Allow at least this long for a batch (ms),
        maxBatchTime        Don't allow a batch to run longer than this (ms),
        reconnectTimeout    Reconnect the socket after this long with no data,
        burstLimit          Read a maximum of this many messages between
                            timer checks

        subscriber          ZMQ socket we're using
        lastRecv            time of the last receive (or 0)
    """

    uri = 'tcp://eddn.edcd.io:9500'
    supportedSchema = 'https://eddn.edcd.io/schemas/commodity/3'

    def __init__(
        self,
        zmqContext=None,
        minBatchTime=36.,       # seconds
        maxBatchTime=60.,       # seconds
        reconnectTimeout=30.,  # seconds
        burstLimit=500,
    ):
        assert burstLimit > 0
        if not zmqContext:
            zmqContext = zmq.Context()
        self.zmqContext = zmqContext
        self.subscriber = None

        self.minBatchTime = minBatchTime
        self.maxBatchTime = maxBatchTime
        self.reconnectTimeout = reconnectTimeout
        self.burstLimit = burstLimit

        self.connect()


    def connect(self):
        """
        Start a connection
        """
        # tear up the new connection first
        if self.subscriber:
            self.subscriber.close()
            del self.subscriber
        self.subscriber = newsub = self.zmqContext.socket(zmq.SUB)
        newsub.setsockopt(zmq.SUBSCRIBE, b"")
        newsub.connect(self.uri)
        self.lastRecv = time.time()
        self.lastJsData = None


    def disconnect(self):
        del self.subscriber


    def wait_for_data(self, softCutoff, hardCutoff):
        """
        Waits for data until maxBatchTime ms has elapsed
        or cutoff (absolute time) has been reached.
        """

        now = time.time()

        cutoff = min(softCutoff, hardCutoff)
        if self.lastRecv < now - self.reconnectTimeout:
            self.connect()
            now = time.time()

        nextCutoff = min(now + self.minBatchTime, cutoff)
        if now > nextCutoff:
            return False

        timeout = (nextCutoff - now) * 1000     # milliseconds

        # Wait for an event
        events = self.subscriber.poll(timeout=timeout)
        if events == 0:
            return False
        return True


    def get_batch(self, queue):
        """
        Greedily collect deduped prices from the firehose over a
        period of between minBatchTime and maxBatchTime, with
        built-in auto-reconnection if there is nothing from the
        firehose for a period of time.

        As json data is decoded, it is stored in self.lastJsData.

        Validated market list messages are added to the queue.
        """
        while go:
            now = time.time()
            hardCutoff = now + self.maxBatchTime
            softCutoff = now + self.minBatchTime

            # hoists
            supportedSchema = self.supportedSchema
            sub = self.subscriber

            # Prices are stored as a dictionary of
            # (sys,stn,item) => [MarketPrice]
            # The list thing is a trick to save us having to do
            # the dictionary lookup twice.
            batch = defaultdict(list)

            if self.wait_for_data(softCutoff, hardCutoff):
                # When wait_for_data returns True, there is some data waiting,
                # possibly multiple messages. At this point we can afford to
                # suck down whatever is waiting in "nonblocking" mode until
                # we reach the burst limit or we get EAGAIN.
                bursts = 0
                for _ in range(self.burstLimit):
                    self.lastJsData = None
                    try:
                        zdata = sub.recv(flags=zmq.NOBLOCK, copy=False)
                    except zmq.error.Again:
                        continue
                    except zmq.error.ZMQError:
                        pass

                    self.lastRecv = time.time()
                    bursts += 1

                    try:
                        jsdata = zlib.decompress(zdata)
                    except Exception:
                        continue

                    bdata = jsdata.decode()

                    try:
                        data = json.loads(bdata)
                    except ValueError:
                        continue

                    self.lastJsData = jsdata

                    try:
                        schema = data["$schemaRef"]
                    except KeyError:
                        continue
                    if schema != supportedSchema:
                        continue
                    try:
                        header = data["header"]
                        message = data["message"]
                        system = message["systemName"].upper()
                        station = message["stationName"].upper()
                        commodities = message["commodities"]
                        timestamp = message["timestamp"]
                        uploader = header["uploaderID"]
                        software = header["softwareName"]
                        swVersion = header["softwareVersion"]
                    except (KeyError, ValueError):
                        continue
                    whitelist_match = list(filter(lambda x: x.get('software').lower() == software.lower(), config['whitelist']))
                    # Upload software not on whitelist is ignored.
                    if len(whitelist_match) == 0:
                        if config['debug']:
                            with debugPath.open('a', encoding = "utf-8") as fh:
                                fh.write(system + "/" + station + " rejected with:" + software + swVersion +"\n")
                        continue
                    # Upload software with version less than the defined minimum is ignored. 
                    if whitelist_match[0].get("minversion"):
                        if LooseVersion(swVersion) < LooseVersion(whitelist_match[0].get("minversion")):
                            if config['debug']:
                                with debugPath.open('a', encoding = "utf-8") as fh:
                                    fh.write(system + "/" + station + " rejected with:" + software + swVersion +"\n")
                            continue
                    # We've received real data.

                    # Normalize timestamps
                    timestamp = timestamp.replace("T"," ").replace("+00:00","")

                    # We'll get either an empty list or a list containing
                    # a MarketPrice. This saves us having to do the expensive
                    # index operation twice.
                    oldEntryList = batch[(system, station)]
                    if oldEntryList:
                        if oldEntryList[0].timestamp > timestamp:
                            continue
                    else:
                        # Add a blank entry to make the list size > 0
                        oldEntryList.append(None)

                    # Here we're replacing the contents of the list.
                    # This simple array lookup is several hundred times less
                    # expensive than looking up a potentially large dictionary
                    # by STATION/SYSTEM:ITEM...
                    oldEntryList[0] = MarketPrice(
                        system, station, commodities,
                        timestamp,
                        uploader, software, swVersion,
                    )

                # For the edge-case where we wait 4.999 seconds and then
                # get a burst of data: stick around a little longer.
                if bursts >= self.burstLimit:
                    softCutoff = min(softCutoff, time.time() + 0.5)


                for entry in batch.values():
                    queue.append(entry[0])
        print("Shutting down listener.")
        self.disconnect()
        
# End of 'kfsone' code.

def db_execute(db, sql_cmd, args = None):
    cur = db.cursor()
    success = False
    result = None
    while go and not success:
        try:
            if args:
                result = cur.execute(sql_cmd, args)
            else:
                result = cur.execute(sql_cmd)
            success = True
        except sqlite3.OperationalError as e:
                if "locked" not in str(e):
                    success = True
                    raise sqlite3.OperationalError(e)
                else:
                    print("Database is locked, waiting for access.", end = "\r")
                    time.sleep(1)
    return result
    

# We do this because the Listener object must be in the same thread that's running get_batch().
def get_messages():
    listener = Listener()
    listener.get_batch(q)

def check_update():
    global update_busy, db_name, item_ids, system_ids, station_ids
    
    # Convert the number from the "check_update_every_x_sec" setting, which is in seconds,
    # into easily readable hours, minutes, seconds.
    m, s = divmod(config['check_update_every_x_sec'], 60)
    h, m = divmod(m, 60)
    next_check = ""
    if h > 0:
        next_check = str(h) + " hour"
        if h > 1:
            next_check += "s"
        if m > 0 or s > 0:
            next_check += ", "
    if m > 0:
        next_check += str(m) + " minute"
        if m > 1:
            next_check += "s"
        if s > 0:
            next_check += ", "                    
    if s > 0:
        next_check += str(s) + " second"
        if s > 1:
            next_check += "s"
            
    # The following values only need to be assigned once, no need to be in the while loop.
    BASE_URL = plugins.eddblink_plug.BASE_URL
    FALLBACK_URL = plugins.eddblink_plug.FALLBACK_URL
    LISTINGS = "listings.csv"
    listings_path = Path(LISTINGS)
    Months = {'Jan':1, 'Feb':2, 'Mar':3, 'Apr':4, 'May':5, 'Jun':6, 'Jul':7, 'Aug':8, 'Sep':9, 'Oct':10, 'Nov':11, 'Dec':12}
       
    while go:
        now = time.time()
    
        dumpModded = 0
        localModded = 0
        
        # We want to get the files from Tromador's mirror, but if it's down we'll go to EDDB.io directly, instead.         
        if config['side'] == 'client':
            try:
                request.urlopen(BASE_URL + LISTINGS)
                url = BASE_URL + LISTINGS
            except:
                url = FALLBACK_URL + LISTINGS
        else:
            url = FALLBACK_URL + LISTINGS

        # Need to parse the "Last-Modified" header into a Unix-epoch, and Python's strptime()
        # won't work because it is locale-dependent, meaning it would only work in English-
        # speaking countries.
        dDL = request.urlopen(url).getheader("Last-Modified").split(' ')
        dTL = dDL[4].split(':')

        dumpDT = datetime.datetime(int(dDL[3]), Months[dDL[2]], int(dDL[1]),\
            hour=int(dTL[0]), minute=int(dTL[1]), second=int(dTL[2]),\
            tzinfo=datetime.timezone.utc)
        dumpModded = timegm(dumpDT.timetuple())

        # Now that we have the Unix epoch time of the dump file, get the same from the local file.
        if Path.exists(eddbPath / listings_path):
            localModded = (eddbPath / listings_path).stat().st_mtime
            
        # Trigger daily EDDB update if the dumps have updated since last run.
        # Otherwise, go to sleep for an hour before checking again.
        if localModded < dumpModded:
            # TD will fail with an error if the database is in use while it's trying
            # to do its thing, so we need to make sure that neither of the database
            # editing methods are doing anything before running.
            update_busy = True
            print("EDDB update available, waiting for busy signal acknowledgement before proceeding.")
            while not (process_ack and export_ack):
                pass
            print("Busy signal acknowledged, performing EDDB dump update.")
            options = config['plugin_options']
            if config['side'] == "server":
                options += ",fallback"
            trade.main(('trade.py','import','-P','eddblink','-O',options))
            
            # Since there's been an update, we need to redo all this.
            del db_name, item_ids, system_ids, station_ids
            db_name, item_ids, system_ids, station_ids = update_dicts()
            
            print("Update complete, turning off busy signal.")
            update_busy = False
        else:
            print("Debug to check this stuff is getting updated correctly:")
            print("now: " + str(now) + " time.time(): " + str(time.time()) + " next_check: " + str(now + config['check_update_every_x_sec']))
            print("No update, checking again in "+ next_check + ".")
            while time.time() < now + config['check_update_every_x_sec']:
                if not go:
                    print("Shutting down update checker.")
                    break
                time.sleep(1)
                
def load_config():
    """
    Loads the settings from 'eddblink-listener-configuration.json'. 
    If the config_file does not exist or is missing any settings, 
    the default will be used for any missing setting, 
    and the config_file will be updated to include all settings,
    preserving the existing (if any) settings' current values.
    """
    
    write_config = False
    # Initialize config with default settings.
    # NOTE: Whitespace added for readability.
    config = OrderedDict([\
                            ('side', 'client'),                                                      \
                            ('verbose', True),                                                       \
                            ('debug', False),                                                        \
                            ('plugin_options', "all,skipvend,force"),                                \
                            ('check_update_every_x_sec', 3600),                                      \
                            ('export_every_x_sec', 300),                                             \
                            ('export_path', './data/eddb'),                                          \
                            ('whitelist',                                                            \
                                [                                                                    \
                                    OrderedDict([ ('software', 'E:D Market Connector [Windows]') ]), \
                                    OrderedDict([ ('software', 'E:D Market Connector [Mac OS]')  ]), \
                                    OrderedDict([ ('software', 'E:D Market Connector [Linux]')   ]), \
                                    OrderedDict([ ('software', 'EDDiscovery')                    ]), \
                                    OrderedDict([ ('software', 'eddi'), ('minversion', '2.2')    ])  \
                                ]                                                                    \
                            )                                                                        \
               ])
    
    # Load the settings from the configuration file if it exists.
    if Path.exists(Path("eddblink-listener-config.json")):
        with open("eddblink-listener-config.json", "rU") as fh:
            try:
                temp = json.load(fh, object_pairs_hook=OrderedDict)
                # For each setting in config,
                # if file setting exists and isn't the default,
                # overwrite config setting with file setting.
                for setting in config:
                    if setting in temp:
                        if config[setting] != temp[setting]:
                            config[setting] = temp[setting]
                    else:
                        # If any settings don't exist in the config_file, need to update the file.
                        write_config = True
                if temp.get("check_delay_in_sec"):
                    config["check_update_every_x_sec"] = temp["check_delay_in_sec"]
                    write_config = True
            except:
                # If, for some reason, there's an error trying to load
                # the config_file, treat it as if it doesn't exist.
                write_config = True
    else:
        # If the config_file doesn't exist, need to make it.
        write_config = True
    
        
    # If the config_file doesn't exist, or if it is missing
    # one or more settings (such as might happen in an upgrade),
    # write the current configuration to the file.
    if write_config:
        with open("eddblink-listener-config.json", "w") as config_file:
            json.dump(config, config_file, indent = 4)
            
    # We now have a config that has valid values for all the settings,
    # even if the setting was not found in the config_file, and the 
    # config_file has been updated if necessary with all previously 
    # missing settings set to default values.
    return config

def validate_config():
    """
    Checks to make sure the loaded config contains valid values.
    If it finds any invalid, it marks that as such in the config_file
    so the default value is used on reload, and then reloads the config.
    """
    global config
    valid = True
    with open("eddblink-listener-config.json", "r") as fh:
        config_file = fh.read()
    
    # For each of these settings, if the value is invalid, mark the key.
    config['side'] = config['side'].lower()
    if config['side'] != 'server' and config['side'] != 'client':
        valid = False
        config_file = config_file.replace('"side"','"side_invalid"')
        
    if not isinstance(config["verbose"], bool):
        valid = False
        config_file = config_file.replace('"verbose"','"verbose_invalid"')

    if not isinstance(config["debug"], bool):
        valid = False
        config_file = config_file.replace('"debug"','"debug_invalid"')
        
    # For this one, rather than completely replace invalid values with the default,
    # check to see if any of the values are valid, and keep them, prepnding the
    # default values to the setting if they aren't already in the setting.
    if isinstance(config['plugin_options'], str):
        options = config['plugin_options'].split(',')
        valid_options = ""
        for option in options:
            if option in ['item','system','station','ship','shipvend','upgrade',\
                          'upvend','listings','all','clean','skipvend','force','fallback']:
                if valid_options != "":
                    valid_options += ","
                valid_options += option
            else:
                valid = False
        if not valid:
            if valid_options.find("force") == -1:
                valid_options = "force," + valid_options 
            if valid_options.find("skipvend") == -1:
                valid_options = "skipvend," + valid_options 
            if valid_options.find("all") == -1:
                valid_options = "all," + valid_options 
            config_file = config_file.replace(config['plugin_options'],valid_options)
    else:
        valid = False
        config_file = config_file.replace('"plugin_options"','"plugin_options_invalid"')
        
    if isinstance(config['check_update_every_x_sec'], int):
        if config['check_update_every_x_sec'] < 1:
            valid = False
            config_file = config_file.replace('"check_update_every_x_sec"','"check_update_every_x_sec_invalid"')
    else:
        valid = False
        config_file = config_file.replace('"check_update_every_x_sec"','"check_update_every_x_sec_invalid"')
        
    if isinstance(config['export_every_x_sec'], int):
        if config['export_every_x_sec'] < 1:
            valid = False
            config_file = config_file.replace('"export_every_x_sec"','"export_every_x_sec_invalid"')
    else:
        valid = False
        config_file = config_file.replace('"export_every_x_sec"','"export_every_x_sec_invalid"')
        
    if not Path.exists(Path(config['export_path'])):
        valid = False
        config_file = config_file.replace('"export_path"','"export_path_invalid"')
        
    if not valid:
        # Before we reload the config to set the invalid values back to default,
        # we need to write the changes we made to the file.
        with open("eddblink-listener-config.json", "w") as fh:
            fh.write(config_file)
        config = load_config()

def process_messages():
    global process_ack
    tdb = tradedb.TradeDB(load=False)
    
    while go:
        db = tdb.getDB()
        # Place the database into autocommit mode to avoid issues with 
        # sqlite3 doing automatic transactions.
        db.isolation_level = None
        
        # We don't want the threads interfering with each other,
        # so pause this one if either the update checker or
        # listings exporter report that they're active.
        if update_busy or export_busy:
            print("Message processor acknowledging busy signal.")
            process_ack = True
            while (update_busy or export_busy) and go:
                time.sleep(1)
            process_ack = False
            # Just in case we caught the shutdown command while waiting.
            if not go:
                # Make sure any changes are committed before shutting down.
                #
                # As we are using autocommit, bypass the db.commit() for now
                # by setting success to "True"
                success = True
                while not success:
                    try:
                        db.commit()
                        success = True
                    except sqlite3.OperationalError as e:
                        if "locked" not in str(e):
                            success = True
                            raise sqlite3.OperationalError(e)
                    else:
                        print("(execute) Database is locked, waiting for access.", end = "\r")
                        time.sleep(1)
                    db.close()
                break
            print("Busy signal off, message processor resuming.")

        # Either get the first message in the queue,
        # or go to sleep and wait if there aren't any.
        try:
            entry = q.popleft()
        except IndexError:
            time.sleep(1)
            continue
        
        # Get the station_is using the system and station names.
        system = entry.system.upper()
        station = entry.station.upper()
        # And the software version used to upload the schema.
        software = entry.software
        swVersion = entry.version
        
        try:
            station_id = station_ids[system + "/" + station]
        except KeyError:
            try:
                # Mobile stations are stored in the dict a bit differently.
                station_id = station_ids["MEGASHIP/" + station]
                system_id = system_ids[system]
                print("Megaship station, updating system.", end=" ")
                # Update the system the station is in, in case it has changed.
                try:
                    db_execute(db, """UPDATE Station
                               SET system_id = ?
                               WHERE station_id = ?""",
                            (system_id, station_id))
                except Exception as e:
                    print(e)
            except KeyError as e:
                if config['verbose']:
                    print("ERROR: Not found in Stations: " + system + "/" + station)
                    continue
        
        modified = entry.timestamp.replace('T',' ').replace('Z','')
        commodities= entry.commodities
        
        start_update = datetime.datetime.now()
        items = dict()
        if config['debug']:
            with debugPath.open('a', encoding = "utf-8") as fh:
                fh.write(system + "/" + station + " with station_id '" + str(station_id) + "' updated at " + modified + " using " + software + swVersion + " ---\n")
            
        for commodity in commodities:
            # Get item_id using commodity name from message.
            try:
                name = db_name[commodity['name'].lower()]
            except KeyError:
                if config['verbose']:
                    print("Ignoring rare item: " + commodity['name'])
                continue
            # Some items, mostly salvage items, are found in db_name but not in item_ids
            # (This is entirely EDDB.io's fault.)
            try:
                item_id = item_ids[name]
            except KeyError:
                if config['verbose']:
                    print("EDDB.io does not include likely salvage item: '" + name + "'")
                continue
            
            items[name] = {'item_id':item_id, 
                           'demand_price':commodity['sellPrice'],
                           'demand_units':commodity['demand'],
                           'demand_level':commodity['demandBracket'] if commodity['demandBracket'] != '' else -1,
                           'supply_price':commodity['buyPrice'],
                           'supply_units':commodity['stock'],
                           'supply_level':commodity['stockBracket'] if commodity['stockBracket'] != '' else -1,
                          }
        
        for key in item_ids:
            if key in items:
                entry = items[key]
            else:
                entry = {'item_id':item_ids[key], 
                           'demand_price':0,
                           'demand_units':0,
                           'demand_level':0,
                           'supply_price':0,
                           'supply_units':0,
                           'supply_level':0,
                        }
            
            if config['debug']:
                with debugPath.open('a', encoding = "utf-8") as fh:
                    fh.write("\t" + key + ": " + str(entry) + "\n")
            
            try:
                # Skip inserting blank entries so as to not bloat DB.
                if entry['demand_price'] == 0 and entry['supply_price'] == 0:
                    raise sqlite3.IntegrityError
                db_execute(db, """INSERT INTO StationItem
                            (station_id, item_id, modified,
                             demand_price, demand_units, demand_level,
                             supply_price, supply_units, supply_level, from_live)
                            VALUES ( ?, ?, ?, ?, ?, ?, ?, ?, ?, 1 )""",
                            (station_id, entry['item_id'], modified,
                            entry['demand_price'], entry['demand_units'], entry['demand_level'],
                            entry['supply_price'], entry['supply_units'], entry['supply_level']))
            except sqlite3.IntegrityError:
                try:
                    db_execute(db, """UPDATE StationItem
                                SET modified = ?,
                                 demand_price = ?, demand_units = ?, demand_level = ?,
                                 supply_price = ?, supply_units = ?, supply_level = ?,
                                 from_live = 1
                                WHERE station_id = ? AND item_id = ?""",
                                (modified, 
                                 entry['demand_price'], entry['demand_units'], entry['demand_level'], 
                                 entry['supply_price'], entry['supply_units'], entry['supply_level'],
                                 station_id, entry['item_id']))
                except sqlite3.IntegrityError as e:
                    if config['verbose']:
                        print("Unable to insert or update: '" + commodity + "' Error: " + str(e))
            
            del entry
        
        # Don't try to commit if there are still messages waiting.
        if len(q) == 0:
            # As we are using autocommit, bypass the db.commit() for now
            # by setting success to "True"
            success = True
            while not success:
                try:
                    db.commit()
                    success = True
                except sqlite3.OperationalError:
                    print("Database is locked, waiting for access.", end = "\r")
                    time.sleep(1)
            # Don't close DB until we've committed the changes.
            db.close()

        if config['verbose']:
            print("Market update for " + system + "/" + station\
                  + " finished in " + str(int((datetime.datetime.now() - start_update).total_seconds() * 1000) / 1000) + " seconds.")
        else:
            print( "Updated " + system + "/" + station)

    print("Shutting down message processor.")

def fetchIter(cursor, arraysize=1000):
    """
    An iterator that uses fetchmany to keep memory usage down
    and speed up the time to retrieve the results dramatically.
    """
    while True:
        results = cursor.fetchmany(arraysize)
        if not results:
            break
        for result in results:
            yield result
            
def export_listings():
    """
    Creates a "listings-live.csv" file in "export_path" every X seconds,
    as defined in the configuration file.
    Only runs when program configured as server.
    """
    global export_ack, export_busy

    if config['side'] == 'server':
        tdb = tradedb.TradeDB(load=False)
        db = tdb.getDB()
        listings_file = (Path(config['export_path']).resolve() / Path("listings-live.csv"))
        listings_tmp = listings_file.with_suffix(".tmp")
        print("Listings will be exported to: \n\t" + str(listings_file))

        while go:
            now = time.time()
            # Wait until the time specified in the "export_every_x_sec" config
            # before doing an export, watch for busy signal or shutdown signal
            # while waiting. 
            while time.time() < now + config['export_every_x_sec']:
                if not go:
                    break
                if update_busy:
                    print("Listings exporter acknowledging busy signal.")
                    export_ack = True
                    while update_busy and go:
                        time.sleep(1)
                    export_ack = False
                    # Just in case we caught the shutdown command while waiting.
                    if not go:
                        break
                    print("Busy signal off, listings exporter resuming.")
                    now = time.time()
                time.sleep(1)
            
            # We may be here because we broke out of the waiting loop,
            # so we need to see if we lost go and quit the main loop if so. 
            if not go:
                break

            start = datetime.datetime.now()

            print("Listings exporter sending busy signal. " + str(start))
            export_busy = True
            # We don't need to wait for acknowledgement from the update checker,
            # because it waits for one from this, and this won't acknowledge
            # until it's finished exporting.
            while not (process_ack):
                if not go:
                    break
            print("Busy signal acknowledged, getting listings for export.")
            try:
                results = list(fetchIter(db_execute(db, "SELECT * FROM StationItem WHERE from_live = 1 ORDER BY station_id, item_id")))
            except sqlite3.DatabaseError as e:
                print(e)
                export_busy = False
                continue
            export_busy = False
            
            print("Exporting 'listings-live.csv'. (Got listings in " + str(datetime.datetime.now() - start) + ")")
            #with open(str(listings_tmp), "w") as f:
            #    f.write("id,station_id,commodity_id,supply,supply_bracket,buy_price,sell_price,demand,demand_bracket,collected_at\n")
            listings_string = "id,station_id,commodity_id,supply,supply_bracket,buy_price,sell_price,demand,demand_bracket,collected_at\n"
            lineNo = 1
            for result in results:
                    # If we lose go during export, we need to abort.
                    if not go:
                        break
                    station_id = str(result[0])
                    commodity_id = str(result[1])
                    sell_price = str(result[2])
                    demand = str(result[3])
                    demand_bracket = str(result[4])
                    buy_price = str(result[5])
                    supply = str(result[6])
                    supply_bracket = str(result[7])
                    collected_at = str(timegm(datetime.datetime.strptime(result[8],'%Y-%m-%d %H:%M:%S').timetuple()))
                    listing = station_id + "," + commodity_id + ","\
                             + supply + "," + supply_bracket + "," + buy_price + ","\
                             + sell_price + "," + demand + "," + demand_bracket + ","\
                             + collected_at
                    #f.write(str(lineNo) + "," + listing + "\n")
                    listings_string += str(lineNo) + "," + listing + "\n"
                    lineNo += 1
            del results
            
            with open(str(listings_tmp), "w") as f:
                f.write(listings_string)
            del listings_string
            
            # If we aborted the export because we lost go, listings_tmp is broken and useless, so delete it. 
            if not go:
                listings_tmp.unlink()
                print("Export aborted, received shutdown signal.")
                break
            
            while listings_file.exists():
                try:
                    listings_file.unlink()
                except:
                    time.sleep(1)
            listings_tmp.rename(listings_file)
            print("Export completed in " + str(datetime.datetime.now() - start))

        print("Shutting down listings exporter.")

    else:
        export_ack = True

def update_dicts():
    # We'll use this to convert the name of the items given in the EDDN messages into the names TD uses.
    db_name = dict()
    edmc_source = 'https://raw.githubusercontent.com/Marginal/EDMarketConnector/master/commodity.csv'
    edmc_csv = request.urlopen(edmc_source)
    edmc_dict = csv.DictReader(codecs.iterdecode(edmc_csv, 'utf-8'))
    for line in iter(edmc_dict):
        db_name[line['symbol'].lower()] = line['name']
    #A few of these don't match between EDMC and EDDB, so we fix them individually.
    db_name['airelics'] = 'Ai Relics'
    db_name['drones'] = 'Limpet'
    db_name['liquidoxygen'] = 'Liquid Oxygen'
    db_name['methanolmonohydratecrystals'] = 'Methanol Monohydrate'
    db_name['coolinghoses'] = 'Micro-Weave Cooling Hoses'
    db_name['nonlethalweapons'] = 'Non-lethal Weapons'
    db_name['sap8corecontainer'] = 'Sap 8 Core Container'
    db_name['trinketsoffortune'] = 'Trinkets Of Hidden Fortune'
    db_name['wreckagecomponents'] = 'Salvageable Wreckage'
    
    # We'll use this to get the item_id from the item's name because it's faster than a database lookup.
    item_ids = dict()
    with open(str(dataPath / Path("Item.csv")), "rU") as fh:
        items = csv.DictReader(fh, quotechar="'")
        for item in items:
            item_ids[item['name']] =  int(item['unq:item_id'])
    
    # We're using these for the same reason. 
    system_names = dict()
    system_ids = dict()
    with open(str(dataPath / Path("System.csv")), "rU") as fh:
        systems = csv.DictReader(fh, quotechar="'")
        for system in systems:
            system_names[int(system['unq:system_id'])] = system['name'].upper()
            system_ids[system['name'].upper()] = int(system['unq:system_id'])
    station_ids = dict()
    with open(str(dataPath / Path("Station.csv")), "rU") as fh:
        stations = csv.DictReader(fh, quotechar="'")
        for station in stations:
            # Mobile stations can move between systems. The mobile stations 
            # have the following data in their entry in stations.jsonl:
            # "type_id":19,"type":"Megaship"
            # Except for that one Orbis station.
            if int(station['type_id']) == 19 or int(station['unq:station_id']) == 42041:
                full_name = "MEGASHIP"
            else:
                full_name = system_names[int(station['system_id@System.system_id'])]
            full_name += "/" + station['name'].upper()
            station_ids[full_name] = int(station['unq:station_id'])
    
    del system_names
    
    return db_name, item_ids, system_ids, station_ids

go = True
q = deque()
config = load_config()
validate_config()

listener_thread = threading.Thread(target=get_messages)
update_thread = threading.Thread(target=check_update)
process_thread = threading.Thread(target=process_messages)
export_thread = threading.Thread(target=export_listings)

# The sooner the listener thread is started, the sooner
# the messages start pouring in.
print("Starting listener.")
listener_thread.start()

# First, check to make sure that EDDBlink plugin has made the changes
# that need to be made for this thing to work correctly.
tdb = tradedb.TradeDB(load=False)
with tdb.sqlPath.open('r', encoding = "utf-8") as fh:
    tmpFile = fh.read()

firstRun = (tmpFile.find('system_id INTEGER PRIMARY KEY AUTOINCREMENT') != -1)

if firstRun:
    # EDDBlink plugin has not made the changes, time to fix that.
    print("EDDBlink plugin has not been run at least once, running now.")
    print("command: 'python trade.py import -P eddblink -O clean,skipvend'")
    trade.main(('trade.py','import','-P','eddblink','-O','clean,skipvend'))
    print("Finished running EDDBlink plugin, no need to run again.")

else:
    print("Running EDDBlink to perform any needed updates.")
    options = 'item'
    if config['side'] == 'server':
        options += ',fallback'
    trade.main(('trade.py','import','-P','eddblink','-O',options))
    # Check to see if plugin updated database.
    with tdb.sqlPath.open('r', encoding = "utf-8") as fh:
        tmpFile = fh.read()
    if tmpFile.find("type_id INTEGER DEFAULT 0 NOT NULL,") == -1:
        sys.exit("EDDBlink plugin must be updated for listener to work correctly.")

update_busy = False
process_ack = False
export_ack = False
export_busy = False

dataPath = Path(tradeenv.TradeEnv().dataDir).resolve()
eddbPath = plugins.eddblink_plug.ImportPlugin(tdb, tradeenv.TradeEnv()).dataPath.resolve()
debugPath = eddbPath / Path("debug.txt")

db_name, item_ids, system_ids, station_ids = update_dicts()

print("Press CTRL-C at any time to quit gracefully.")
try:
    update_thread.start()
    # Give the update checker enough time to see if an update is needed,
    # before starting the message processor and listings exporter.
    time.sleep(5)
    process_thread.start()
    export_thread.start()

    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("CTRL-C detected, stopping.")
    if config['side'] == 'server':
        print("Please wait for all four processes to report they are finished, in case they are currently active.")
    else:
        print("Please wait for all three processes to report they are finished, in case they are currently active.")
    go = False