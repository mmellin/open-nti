#!/usr/bin/env python
# coding: utf-8
# Authors: efrain@juniper.net psagrera@juniper.net
# Version 2.0  20160124

from datetime import datetime # In order to retreive time and timespan
from datetime import timedelta # In order to retreive time and timespan
from influxdb import InfluxDBClient
from pyez_mock import mocked_device, rpc_reply_dict
from jnpr.junos import *
from jnpr.junos import Device
from jnpr.junos.exception import *
from jnpr.junos.utils.start_shell import StartShell
from lxml import etree  # Used for xml manipulation
from pprint import pformat
from pprint import pprint
import argparse   # Used for argument parsing
import json
import logging
import logging.handlers
import os  # For exec to work
import pprint
import re # For regular expression usage
import requests
import string
import string  # For split multiline script into multiple lines
import StringIO   # Used for file read/write
import sys  # For exec to work
import threading
import time
import xmltodict
import yaml
import copy
import collections

logging.getLogger("paramiko").setLevel(logging.INFO)
logging.getLogger("ncclient").setLevel(logging.WARNING) # In order to remove http request from ssh/paramiko
logging.getLogger("requests").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)  # In order to remove http request from InfluxDBClient

####################################################################################
####################################################################################
# Defining the classes and procedures used later on the script
####################################################################################
####################################################################################

def convert_variable_type(var):

    try:
        result = int(var)
        return result
    except Exception as e:
        pass

    try:
        result = float(var)
        return result
    except Exception as e:
        pass
    return var   # I guess that is a string

def check_db_status():
    # if the db is not found, then try to create it
    try:
        dbclient = InfluxDBClient(db_server, db_port, db_admin, db_admin_password)
        dblist = dbclient.get_list_database()
        db_found = False
        for db in dblist:
            if db['name'] == db_name:
                db_found = True
        if not(db_found):
            logger.info('Database <%s> not found, trying to create it', db_name)
            dbclient.create_database(db_name)
        return True
    except Exception as e:
        logger.error('Error querying open-nti database: %s', e)
        return False

def get_latest_datapoints(**kwargs):

    dbclient = InfluxDBClient(db_server, db_port, db_admin, db_admin_password)
    dbclient.switch_database(db_name)
    results = {}
    if db_schema == 1:
        query = "select * from /%s\./ GROUP BY * ORDER BY time DESC limit 1 " % (kwargs['host'])
    elif db_schema == 2:
        query = "select * from \"%s\" WHERE device = '%s' GROUP BY * ORDER BY time DESC limit 1 " % ('jnpr.collector',kwargs['host'])
    elif db_schema == 3:
        query = "select * from // WHERE device = '%s' GROUP BY * ORDER BY time DESC limit 1 " % (kwargs['host'])
    else:
        logger.error("ERROR: Unknown db_schema: <%s>", db_schema)
        return results

    results = dbclient.query(query)
    return results

def get_target_hosts():
    my_target_hosts = {}
    for host in sorted(hosts.keys()):
        for tag in tag_list:
            for hosts_tag in hosts[host].split():
                if re.search(tag, hosts_tag, re.IGNORECASE):
                    my_target_hosts[host] = 1
    return my_target_hosts.keys()

def get_target_commands(my_host):
    my_host_tags = hosts[my_host]
    my_target_commands = {}
    for group_command in sorted(general_commands.keys()):
        for my_host_tag in my_host_tags.strip().split():
            for command_tag in general_commands[group_command]["tags"].split():
                if re.search(my_host_tag, command_tag, re.IGNORECASE):
                    if "commands" in general_commands[group_command].keys():
                        for cmd in general_commands[group_command]["commands"].strip().split("\n"):
                            my_target_commands[cmd] = 1
    return my_target_commands.keys()

def get_credentials(my_host):
    my_host_tags = hosts[my_host]
    my_target_credentials = {}
    for credential in sorted(credentials.keys()):
        for my_host_tag in my_host_tags.strip().split():
            for credential_tag in credentials[credential]["tags"].split():
                if re.search(my_host_tag, credential_tag, re.IGNORECASE):
                    if ("username" in credentials[credential].keys()):
                        if ("method" in credentials[credential].keys()):
                            if (credentials[credential]["method"] == "key"):
                                if ("key_file" in credentials[credential].keys()):
                                    return credentials[credential]["username"], "", credentials[credential]["method"], credentials[credential]["key_file"]
                                else:
                                    logger.error("Missing key_file information")
                                    sys.exit(0)

                            elif (credentials[credential]["method"] == "enc_key"):
                                if ("key_file" in credentials[credential].keys()):
                                    if ("password" in credentials[credential].keys()):
                                        return credentials[credential]["username"], credentials[credential]["password"], credentials[credential]["method"], credentials[credential]["key_file"]
                                    else:
                                        logger.error("Missing password information")
                                        sys.exit(0)
                                else:
                                    logger.error("Missing key_file information")
                            elif (credentials[credential]["method"] == "password"):
                                return credentials[credential]["username"], credentials[credential]["password"], credentials[credential]["method"], ""
                            else:
                                logger.error("Unknown authentication method found")
                                sys.exit(0)
                        else:
                            if ("password" in credentials[credential].keys()):
                                return credentials[credential]["username"], credentials[credential]["password"], "password", ""
                            else:
                                logger.error("Missing password information")
                                sys.exit(0)
                    else:
                        logger.error("Missing username information")
                        sys.exit(0)

def execute_command(jdevice,command):
    format = "text"
    command_tmp = command
    if re.search("\| display xml", command, re.IGNORECASE):
        format = "xml"
        command_tmp = command.replace("| display xml","")
    elif re.search("\| count", command, re.IGNORECASE):
        format = "txt-filtered"
        command_tmp = command.split("|")[0]
    try:
        # Remember... all rpc must have format=xml at execution time,
        command_result = jdevice.rpc.cli(command_tmp, format="xml")
    except RpcError as err:
        rpc_error = err.__repr__()
        logger.error("Error found executing command: %s, error: %s:", command ,rpc_error)
        return False

    if format == "text":
        # We need to confirm that root tag in command_result is <output> if not then raise exception and skip
        return command_result.text
    elif format == "xml":
        return etree.tostring(command_result)
    elif format == "txt-filtered":
        operations = command.split("|")[1:]
        result_tmp = command_result.text
        lines=result_tmp.strip().split('\n')
        for operation in operations:
            logger.info("Processing <%s>", operation )
            if re.search("count", operation, re.IGNORECASE):
                result = "Count: %s lines" % len(lines)
                logger.debug("Count result: <%s>", result )
                return result
            match = re.search("match (.*)", operation, re.IGNORECASE)
            if match:
                regex = match.group(1).strip()
                logger.debug("Found regex: <%s>", regex )
                lines_filtered = []
                for line in lines:
                    if re.search(regex, line, re.IGNORECASE):
                        lines_filtered.append(line)
                lines = lines_filtered
                logger.debug("Filtered result:\n%s", "\n".join(lines_filtered) )
            match = re.search("except (.*)", operation, re.IGNORECASE)
            if match:
                regex = match.group(1).strip()
                logger.debug("Found regex: <%s>", regex )
                lines_filtered = []
                for line in lines:
                    if re.search(regex, line, re.IGNORECASE):
                        pass
                    else:
                        lines_filtered.append(line)
                lines = lines_filtered
                logger.debug("Filtered result:\n%s", "\n".join(lines_filtered) )
        return "\n".join(lines)

def eval_variable_name(variable,**kwargs):
    keys={}
    if 'keys' in kwargs.keys():
        # This is due dict are mutable and a normal assigment does NOT copy the value, it copy the reference
        keys=copy.deepcopy(kwargs['keys'])
    if db_schema == 3:
        for key in keys.keys():
            variable = variable.replace("$"+key,"")
            variable = variable.replace("..",".")
        variable = variable.replace("$host","")
        variable = re.sub(r"^\.", "", variable)
        return variable, variable
    if db_schema == 2:
        for key in keys.keys():
            variable = variable.replace("$"+key,"")
            variable = variable.replace("..",".")
        variable = variable.replace("$host","")
        variable = re.sub(r"^\.", "", variable)
        return "jnpr.collector", variable
    else: # default db_schema (option 1) open-nti legacy
        for key in keys.keys():
            variable = variable.replace("$"+key,keys[key])
        variable = variable.replace("$host",kwargs['host'])
        # the host replacement should be move it to other place
        print variable
        return variable, variable

def eval_tag_name(variable,**kwargs):
    for key in kwargs:
        variable = variable.replace("$"+key,kwargs[key])
    return variable

def eval_variable_value(value,**kwargs):

    if (kwargs["type"] == "integer"):
        value =  re.sub('G','000000000',value)
        value =  re.sub('M','000000',value)
        value =  re.sub('K','000',value)
        return(int(float(value)))
    elif kwargs["type"] == "string":
        return value
    else:
        logger.error('An unkown variable-type found: %s', kwargs["type"])
        return value

def insert_datapoints(datapoints):

    dbclient = InfluxDBClient(db_server, db_port, db_admin, db_admin_password)
    dbclient.switch_database(db_name)
    logger.info('Inserting into database the following datapoints:')
    logger.info(pformat(datapoints))
    response = dbclient.write_points(datapoints)

def get_metadata_and_add_datapoint(datapoints,**kwargs):

    value_tmp  = kwargs['value_tmp']
    host = kwargs['host']
    latest_datapoints=kwargs['latest_datapoints']

    match={}
    if 'match' in kwargs.keys():
        # This is due dict are mutable and a normal assigment does NOT copy the value, it copy the reference
        match=copy.deepcopy(kwargs['match'])

    kpi_tags={}
    if 'kpi_tags' in kwargs.keys():
        # This is due dict are mutable and a normal assigment does NOT copy the value, it copy the reference
        kpi_tags=copy.deepcopy(kwargs['kpi_tags'])

    #    Need to double check if with latest improvements key variable is not used anymore
    #    key=''
    #    if 'key' in kwargs.keys():
    #        key=kwargs['key']

    keys={}
    if 'keys' in kwargs.keys():
        # This is due dict are mutable and a normal assigment does NOT copy the value, it copy the reference
        keys=copy.deepcopy(kwargs['keys'])
        for key_name in keys.keys():
            kpi_tags[key_name] = keys[key_name]

    # Resolving the variable name
    value = convert_variable_type(value_tmp)
    variable_name, kpi_tags['kpi'] = eval_variable_name(match["variable-name"],host=host,keys=keys)

    # Calculating delta values (only applies for numeric values)
    delta = 0
    latest_value = ''
    if (type (value) != str):

        points=[]
        if (db_schema == 1):
            points = list(latest_datapoints.get_points(measurement = variable_name))
        elif (db_schema == 2):
            points = list(latest_datapoints.get_points(measurement = 'jnpr.collector', tags=kpi_tags))
        elif (db_schema == 3):
            points = list(latest_datapoints.get_points(measurement = kpi_tags['kpi'], tags=kpi_tags))
        else:
            logger.error("ERROR: Unknown db_schema: <%s>", db_schema)

        if len(points) == 1:
            latest_value = points[0]['value']
            delta = value - convert_variable_type(latest_value)
            logger.debug("Delta found : points <%s> latest_value <%s>", points,latest_value)
        elif len(points) == 0:
            delta = value
            logger.debug("No latest datapoint found for <%s>", kpi_tags)
        else:
            logger.error("ERROR: Latest datapoint query returns more than one match : <%s>", points)

        if type (value) == int:
            delta = int(delta)
        elif type (value) == float:
            delta = float(delta)
    else:
        delta = 'N/A'

    # Getting all tags related to the kpi
    #    if 'tags' in match.keys():
    #        for tag in match['tags']:
    #            tag_name = tag.keys()[0]  # We asume that this dict only has one key
    #            tag_value = eval_tag_name(tag[tag_name],host=host,key=key)
    #            # this need to be updated when there is more than one key
    #            kpi_tags[tag_name] = tag_value

    # Building the kpi and append it to the other kpis before database insertion
    if type (value) != str:
        kpi = {
            "measurement": variable_name,
            "fields": {
                "value": value,
                "delta": delta
            }
        }
    else:
        kpi = {
            "measurement": variable_name,
            "fields": {
                "value_str": value,
                "delta_str": delta
            }
        }

    kpi["tags"] = kpi_tags
    datapoints.append(copy.deepcopy(kpi))

def parse_result(host,target_command,result,datapoints,latest_datapoints,kpi_tags):
    parser_found = False
    for junos_parser in junos_parsers:
        regex_command = junos_parser["parser"]["regex-command"]
        if re.search(regex_command, target_command, re.IGNORECASE):
            parser_found = True
            matches = junos_parser["parser"]["matches"]
            timestamp = str(int(datetime.today().strftime('%s')))
            for match in matches:
                try:
                    if match["method"] == "xpath":
                        # Load xml data
                        xml_data = etree.fromstring(result)
                        if match["type"] == "single-value":
                            try:
                                logger.debug('[%s]: Looking for a match: %s', host, match["xpath"])
                                if xml_data.xpath(match["xpath"]):
                                    value_tmp = xml_data.xpath(match["xpath"])[0].text.strip()
                                    get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)
                                else:
                                    logger.debug('No match found: %s', match["xpath"])
                                    if 'default-if-missing' in match.keys():
                                        logger.debug('Inserting default-if-missing value: %s', match["default-if-missing"])
                                        value_tmp = match["default-if-missing"]
                                        get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)
                            except Exception, e:
                                logger.info('[%s]: Exception found.', host)
                                logging.exception(e)
                                pass  # Notify about the specific problem with the host BUT we need to continue with our list
                        elif match["type"] == "multi-value":
                            nodes = xml_data.xpath(match["xpath"])
                            for node in nodes:
                                #Look for all posible keys or fields to extract and be used for variable-naming
                                #key = node.xpath(match["loop"]["key"])[0].text.replace(" ","_").strip()
                                #print "the key is: " + key
                                keys = {}
                                keys_tmp = copy.deepcopy(match["loop"])
                                #print keys_tmp
                                if 'sub-matches' in keys_tmp.keys():
                                    del keys_tmp['sub-matches']
                                for key_tmp in keys_tmp.keys():
                                    keys[key_tmp]=node.xpath(keys_tmp[key_tmp])[0].text.replace(" ","_").strip()
                                #print keys
                                for sub_match in match["loop"]["sub-matches"]:
                                    try:
                                        logger.debug('[%s]: Looking for a sub-match: %s', host, sub_match["xpath"])
                                        if node.xpath(sub_match["xpath"]):
                                            if "regex" in sub_match:
                                                value_tmp = node.xpath(sub_match["xpath"])[0].text.strip()
                                                regex = sub_match["regex"]
                                                text_matches = re.search(regex,value_tmp,re.MULTILINE)
                                                if text_matches:
                                                    if text_matches.lastindex == len(sub_match["variables"]):
                                                        logger.debug('[%s]: We have (%s) matches with this regex %s', host, text_matches.lastindex,regex)
                                                        for i in range(0,text_matches.lastindex):
                                                            j=i+1
                                                            variable_name = eval_variable_name(sub_match["variables"][i]["variable-name"],host=host)
                                                            value_tmp = text_matches.group(j).strip()
                                                            # Begin function  (pero pendiente de ver si variable-type existe y su valor)
                                                            if "variable-type" in sub_match["variables"][i]:
                                                                value_tmp = eval_variable_value(value_tmp, type=sub_match["variables"][i]["variable-type"])
                                                            get_metadata_and_add_datapoint(datapoints=datapoints,match=sub_match["variables"][i],value_tmp=value_tmp,host=host,latest_datapoints=latest_datapoints,kpi_tags=kpi_tags,keys=keys)
                                                    else:
                                                        logger.error('[%s]: More matches found on regex than variables especified on parser: %s', host, regex_command)
                                                else:
                                                    logger.debug('[%s]: No matches found for regex: %s', host, regex)

                                            else:
                                                value_tmp = node.xpath(sub_match["xpath"])[0].text.strip()
                                                get_metadata_and_add_datapoint(datapoints=datapoints,match=sub_match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags,keys=keys)
                                        else:
                                            logger.debug('[%s]: No match found: %s', host, match["xpath"])
                                            if 'default-if-missing' in sub_match.keys():
                                                logger.debug('Inserting default-if-missing value: %s', sub_match["default-if-missing"])
                                                value_tmp = sub_match["default-if-missing"]
                                                get_metadata_and_add_datapoint(datapoints=datapoints,match=sub_match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags,keys=keys)
                                    except Exception, e:
                                        logger.info('[%s]: Exception found.', host)
                                        logging.exception(e)
                                        pass  # Notify about the specific problem with the host BUT we need to continue with our list
                        else:
                            logger.error('[%s]: An unknown match-type found in parser with regex: %s', host, regex_command)
                    elif match["method"] == "regex": # we need to evaluate a text regex
                        if match["type"] == "single-value":
                            regex = match["regex"]
                            text_matches = re.search(regex,result,re.MULTILINE)
                            if text_matches:
                                if text_matches.lastindex == len(match["variables"]):
                                    logger.debug('[%s]: We have (%s) matches with this regex %s', host, text_matches.lastindex,regex)
                                    for i in range(0,text_matches.lastindex):
                                        j=i+1
                                        variable_name = eval_variable_name(match["variables"][i]["variable-name"],host=host)
                                        value_tmp = text_matches.group(j).strip()
                                        # Begin function  (pero pendiente de ver si variable-type existe y su valor)
                                        if "variable-type" in match["variables"][i]:
                                            value_tmp = eval_variable_value(value_tmp, type=match["variables"][i]["variable-type"])
                                        get_metadata_and_add_datapoint(datapoints=datapoints,match=match["variables"][i],value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)
                                else:
                                    logger.error('[%s]: More matches found on regex than variables especified on parser: %s', host, regex_command)
                            else:
                                logger.debug('[%s]: No matches found for regex: %s', host, regex)
                        elif match["type"] == "multi-value":
                            '''
                            New multi-value statement allowing for multiple identical regex matches parsed from a string.
                            The idea here is if you want to parse a multiline string using regex and return X number of matches
                            instead of the usual single match.  This is useful for capturing, say, the top 5 highest CPU performers
                            in the "show system processes extensive" command each time you call the command.  Used in blackbox style
                            testing to see which daemons are contributors to high CPU although you might not know the names, hence are
                            unable to add them into the YAML one by one.

                            Each re.search result will capture more than one set of rows if mcount >1, so you'll need a "key" to figure out
                            which re.search.group() will be equal to a newline and be able to split the output into the appropriate
                            sub-outputs for parsing.

                            Example psuedo code:
                                input multiline string from XML:
                                "10 root        1 155   52     0K    12K RUN    594:20 93.80% idle

                                33794 root        1  40    0 43036K 33116K select   0:00  1.75% mgd"

                                match.lastindex = 8
                                match.groups() = ('0K', '12K', '93.80', 'idle', '43036K', '33116K', '1.75', 'mgd')
                                key = match.groups([3]) and match.groups([7])
                                Use the Key as the daemon name and create a measurement name with it.  All other values
                                are field values.

                            '''
                            mcount = match["match_count"]
                            regex_block = match["regex"]
                            regex_framework = "{0}" * mcount
                            regex = regex_framework.format(regex_block)
                            text_matches = re.search(regex,result,re.MULTILINE)
                            if text_matches:
                                '''We want to check the number of variables in the yaml is equal to the number of variables/groups
                                in the regex minus one (for the group's key).
                                Example: 20 total matches, divided by mcount 5 gives us 4 matches per group, minus 1 for the key
                                which tells us there are 5 regex output groups we want, each 4 members long with a key
                                which we need to subtract out.  This gives us 3 variables expected in our yaml list.
                                '''
                                # Total number of regex groups per match
                                regexmatch_group_count = (text_matches.lastindex / mcount)
                                # Number of variables should be total number of groups minus the index for the group data.
                                variable_count = regexmatch_group_count - 1
                                if variable_count == len(match["variables"]):
                                    logger.debug('[%s]: We have (%s) matches and match_count of (%s) with this regex %s', host, text_matches.lastindex,mcount,regex)
                                    match_groups ={}
                                    try:
                                        key_index = int(match["key_index"])
                                        if key_index >= 1:
                                            # Used because regex output is a tuple, and indexes start at 0, not 1.
                                            # Tells is the location of our "index" value (aka- measurement name) within the tuple.
                                            tuple_index = key_index - 1
                                        else:
                                            logger.info('[%s]: Exception found in yaml for key_index = %s. '
                                                        'Key_index is less than 1. Please make key index 1 or greater.', host, match["key_index"])
                                            pass    # Perhaps some other solution is better?
                                    except ValueError as e:
                                        logger.info('[%s]: Exception found in yaml for key_index = %s.  Key_index not an integer', host, match["key_index"])
                                        logging.exception(e)
                                    except Exception, e:
                                        logger.info('[%s]: Exception found.', host)
                                        logging.exception(e)
                                    start_index = 0
                                    for i in range(tuple_index,text_matches.lastindex,regexmatch_group_count):
                                        match_groups[(text_matches.groups()[start_index:i])] = {"key":text_matches.groups()[i]}
                                        start_index += regexmatch_group_count
                                    data_tuples = match_groups.keys()
                                    for tuple in data_tuples:
                                        index_key = match_groups[tuple]
                                        # Add measurements to DB per variable for each group of values.
                                        for i in range(0,variable_count):
                                            variable_name = eval_variable_name(match["variables"][i]["variable-name"],host=host,keys=index_key)
                                            value_tmp = tuple[i].strip()
                                            # Begin function  (pero pendiente de ver si variable-type existe y su valor)
                                            if "variable-type" in match["variables"][i]:
                                                value_tmp = eval_variable_value(value_tmp, type=match["variables"][i]["variable-type"])
                                            get_metadata_and_add_datapoint(datapoints=datapoints,match=match["variables"][i],value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags,keys=index_key)
                                else:
                                    logger.error('[%s]: More matches found on regex than variables specified on parser: %s', host, regex_command)

                            else:
                                logger.debug('[%s]: No matches found for regex: %s', host, regex)
                        else:
                            logger.error('[%s]: An unkown match-type found in parser with regex: %s', host, regex_command)
                    else:
                        logger.error('[%s]: An unkown method found in parser with regex: %s', host, regex_command)
                except Exception, e:
                    logger.info('[%s]: Exception found.', host)
                    logging.exception(e)
                    pass  # Notify about the specific problem with the host BUT we need to continue with our list
        if parser_found:
            logger.info('[%s]: Parser found and processed, going to next comand.', host)
            break
    if (not(parser_found)):
        logger.error('[%s]: ERROR: Parser not found for command: %s', host, target_command)

def collector(**kwargs):

    for host in kwargs["host_list"]:
        kpi_tags={}
        latest_datapoints={}
#        if ((db_schema == 1) and (not(use_hostname))):
        if (not(use_hostname)):
            latest_datapoints = get_latest_datapoints(host=host)
            logger.debug("Latest Datapoints are:")
            logger.debug(pformat(latest_datapoints))

        #    kpi_tags = get_host_base_tags(host=host)
        # Check host tag to identify what kind of connections will be used (ej junos / others  / etc)
        if "non_junos_devices" in hosts[host].split():
            pass
            # Reserved for future purposes
        else: # By default it's a junos device
            # We need to CATCH errors then print then but we need to continue with next host...
            connected = False
            logger.info('Connecting to host: %s', host)
            target_commands = get_target_commands(host)
            timestamp_tracking={}
            timestamp_tracking['collector_start'] = int(datetime.today().strftime('%s'))
            # Establish connection to hosts
            user, passwd, authMethod,authKey_file = get_credentials(host)
            if dynamic_args['test']:
                #Open an emulated Junos device instead of connecting to the real one
                _rpc_reply_dict = rpc_reply_dict()
                _rpc_reply_dict['dir'] = BASE_DIR_INPUT

                jdev = mocked_device(_rpc_reply_dict)
                # First collect all kpi in datapoints {} then at the end we insert them into DB (performance improvement)
                connected = True
            else:
                if authMethod in "key":
                    jdev = Device(user=user, host=host, ssh_private_key_file=authKey_file, gather_facts=False, auto_probe=True, port=22)
                elif authMethod in "enc_key":
                    jdev = Device(user=user, host=host, ssh_private_key_file=authKey_file, password=passwd, gather_facts=False, auto_probe=True, port=22)
                else: # Default is 
                    jdev = Device(user=user, host=host, password=passwd, gather_facts=False, auto_probe=True, port=22)
                for i in range(1, max_connection_retries+1):
                    try:
                        jdev.open()
                        jdev.timeout = default_junos_rpc_timeout
                        connected = True
                        break
                    except Exception, e:
                        if i < max_connection_retries:
                            logger.error('[%s]: Connection failed %s time(s), retrying....', host, i)
                            time.sleep(1)
                            continue
                        else:
                            logging.exception(e)
                            connected = False  # Notify about the specific problem with the host BUT we need to continue with our list
            # First collect all kpi in datapoints {} then at the end we insert them into DB (performance improvement)
            if connected:
                datapoints = []
                # By default execute show version in order to get version and platform as default tags for all kpi related to this host
                kpi_tags = {}
                target_command = 'show version | display xml'
                version_xpath = "//package-information/comment"
                product_model_xpath = "//product-model"
                logger.info('[%s]: Executing command: %s', host, target_command)
                result = execute_command(jdev,target_command)
                if result:
                    logger.debug('[%s]: Parsing command: %s', host, target_command)
                    xml_data = etree.fromstring(result)
                    value_tmp = xml_data.xpath(version_xpath)[0].text.strip()
                    version = re.search('\[(.*?)\]$', value_tmp)
                    if version:
                        kpi_tags['version'] = version.group(1)
                    else:
                        kpi_tags['version'] = 'unknown'
                    value_tmp = xml_data.xpath(product_model_xpath)[0].text.strip()
                    kpi_tags['product-model'] = convert_variable_type(value_tmp)

                    ## Based on parameter defined in config file

                    if use_hostname:
                        hostname_xpth = "//host-name"
                        hostname_tmp = xml_data.xpath(hostname_xpth)[0].text.strip()
                        hostname = convert_variable_type(hostname_tmp)
                        logger.info('[%s]: Host will now be referenced as : %s', host, hostname)
                        host = hostname
#                        if (db_schema == 1):
#                            latest_datapoints = get_latest_datapoints(host=host)
#                            logger.info("Latest Datapoints are:")
#                            logger.info(pformat(latest_datapoints))
                        latest_datapoints = get_latest_datapoints(host=host)
                        logger.debug("Latest Datapoints are:")
                        logger.debug(pformat(latest_datapoints))
                    else:
                        logger.info('[%s]: Host will be referenced as : %s', host, host)


                    kpi_tags['device']=host
                    kpi_tags['kpi']="base-info"
                    match={}
                    match["variable-name"]="base-info"
                    # We'll add a dummy kpi in oder to have at least one fixed kpi with version/platform data.
                    get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)

                # Now we have all hosts tags that all host kpis will inherit
                # For each target_command execute it, parse it, and insert values into DB
                timestamp_tracking['collector_cli_start'] = int(datetime.today().strftime('%s'))
                for target_command in target_commands:
                    logger.info('[%s]: Executing command: %s', host, target_command)
                    # Execute rpc/command on host and get result
                    result = execute_command(jdev,target_command)
                    if result:
                        logger.debug('[%s]: Parsing command: %s', host, target_command)
                        parse_result(host,target_command,result,datapoints,latest_datapoints,kpi_tags)
                        time.sleep(delay_between_commands)

                try:
                    jdev.close()
                    time.sleep(0.5)
                except Exception, e:
                    print "ERROR: Something wrong happens when closing the connection with the device"
                    logging.exception(e)

                timestamp_tracking['collector_cli_ends'] = int(datetime.today().strftime('%s'))
                logger.info('[%s]: timestamp_tracking - CLI collection %s', host, timestamp_tracking['collector_cli_ends']-timestamp_tracking['collector_cli_start'])
                timestamp_tracking['collector_ends'] = int(datetime.today().strftime('%s'))

                # Add open-nti internal kpi 
                collection_time = timestamp_tracking['collector_ends']-timestamp_tracking['collector_start']
                #kpi_tags['device']=host
                kpi_tags['stats']="collection-time"
                match={}
                match["variable-name"]="open-nti-stats"
                value_tmp =collection_time
                # We'll add a dummy kpi in oder to have at least one fixed kpi with version/platform data.
                get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)

                #kpi_tags['device']=host
                kpi_tags['stats']="collection-successful"
                match={}
                match["variable-name"]="open-nti-stats"
                value_tmp = 1
                # We'll add a dummy kpi in oder to have at least one fixed kpi with version/platform data.
                get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)



                if datapoints:   # Only insert datapoints if there is any :)
                    insert_datapoints(datapoints)

                
                logger.info('[%s]: timestamp_tracking - total collection %s', host, collection_time)
            else:
                logger.error('[%s]: Skipping host due connectivity issue', host)

                datapoints = []
                latest_datapoints = get_latest_datapoints(host=host)
                # By default execute show version in order to get version and platform as default tags for all kpi related to this host
                kpi_tags['device']=host
                kpi_tags['stats']="collection-failure"
                match={}
                match["variable-name"]="open-nti-stats"
                value_tmp = 1
                # We'll add a dummy kpi in oder to have at least one fixed kpi with version/platform data.
                get_metadata_and_add_datapoint(datapoints=datapoints,match=match,value_tmp=value_tmp,latest_datapoints=latest_datapoints,host=host,kpi_tags=kpi_tags)

                if datapoints:   # Only insert datapoints if there is any :)
                    insert_datapoints(datapoints)



################################################################################################
################################################################################################
################################################################################################

# SCRIPT STARTS HERE

################################################################################################
# Create and Parse Arguments
################################################################################################

if getattr(sys, 'frozen', False):
    # frozen
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # unfrozen
    BASE_DIR = os.path.dirname(os.path.realpath(__file__))

BASE_DIR_INPUT = BASE_DIR + "/data/"

full_parser = argparse.ArgumentParser()
full_parser.add_argument("--tag", nargs='+', help="Collect data from hosts that matches the tag")
full_parser.add_argument("-c", "--console", action='store_true', help="Console logs enabled")
full_parser.add_argument("-t", "--test", action='store_true', help="Use emulated Junos device")
full_parser.add_argument("-s", "--start", action='store_true', help="Start collecting (default 'no')")
full_parser.add_argument("-i", "--input", default= BASE_DIR_INPUT, help="Directory where to find input files")

dynamic_args = vars(full_parser.parse_args())

## Change BASE_DIR_INPUT if we are in "test" mode
if dynamic_args['test']:
    BASE_DIR_INPUT = dynamic_args['input']

################################################################################################
# Loading YAML Default Variables
###############################################################################################

default_variables_yaml_file = BASE_DIR_INPUT + "open-nti.variables.yaml"
default_variables = {}

try:
    with open(default_variables_yaml_file) as f:
        default_variables = yaml.load(f)
except Exception, e:
    logger.info('Error importing default variables file": %s', default_variables_yaml_file)
    logging.exception(e)
    sys.exit(0)

db_schema = default_variables['db_schema']
db_server = default_variables['db_server']
db_port = default_variables['db_port']
db_name = default_variables['db_name']
db_admin = default_variables['db_admin']
db_admin_password = default_variables['db_admin_password']
db_user = default_variables['db_user']
db_user_password = default_variables['db_user_password']
max_connection_retries = default_variables['max_connection_retries']
max_collector_threads = default_variables['max_collector_threads']
delay_between_commands = default_variables['delay_between_commands']
logging_level = default_variables['logging_level']
default_junos_rpc_timeout = default_variables['default_junos_rpc_timeout']
use_hostname = default_variables['use_hostname']

################################################################################################
# Validate Arguments
###############################################################################################
tag_list = []
###  Known and fixed arguments
if dynamic_args['tag']:
    tag_list = dynamic_args['tag']
else:
    tag_list = [ ".*" ]

if not(dynamic_args['start']):
    logger.error('Missing <start> option, so nothing to do')
    sys.exit(0)

################################################################################################
# open-nti starts here start
################################################################################################

# Setting up logging directories and files
timestamp = time.strftime("%Y-%m-%d", time.localtime(time.time()))
log_dir = BASE_DIR + "/" + default_variables['log_dir']
logger = logging.getLogger("_open-nti_")
if not os.path.exists(log_dir):
    os.makedirs(log_dir, 0755)
formatter = '%(asctime)s %(name)s %(levelname)s %(threadName)-10s:  %(message)s'
logging.basicConfig(filename=log_dir + "/"+ timestamp + '_open-nti.log',level=logging_level,format=formatter, datefmt='%Y-%m-%d %H:%M:%S')

if dynamic_args['console']:
    logger.info("Console logs enabled")
    console = logging.StreamHandler()
    console.setLevel (logging.DEBUG)
    logging.getLogger('').addHandler(console)

###############

#  LOAD all credentials in a dict

credentials_yaml_file = BASE_DIR_INPUT + default_variables['credentials_file']
credentials = {}
logger.debug('Importing credentials file: %s ',credentials_yaml_file)
try:
    with open(credentials_yaml_file) as f:
        credentials = yaml.load(f)
except Exception, e:
    logger.error('Error importing credentials file: %s', credentials_yaml_file)
 #   logging.exception(e)
    sys.exit(0)  
#  LOAD all hosts with their tags in a dic

hosts_yaml_file = BASE_DIR_INPUT + default_variables['hosts_file']
hosts = {}
logger.debug('Importing host file: %s ',hosts_yaml_file)
try:
    with open(hosts_yaml_file) as f:
        hosts = yaml.load(f)
except Exception, e:
    logger.error('Error importing host file: %s', hosts_yaml_file)
    #logging.exception(e)
    sys.exit(0)        


#  LOAD all commands with their tags in a dict

commands_yaml_file = BASE_DIR_INPUT + default_variables['commands_file']
commands = []
logger.debug('Importing commands file: %s ',commands_yaml_file)
with open(commands_yaml_file) as f:
    try:
        for document in yaml.load_all(f):
            commands.append(document)
    except Exception, e:
        logger.error('Error importing commands file: %s', commands_yaml_file)
#        logging.exception(e)
        sys.exit(0)        

general_commands = commands[0]

#  LOAD all parsers

junos_parsers = []
junos_parsers_yaml_files = os.listdir(BASE_DIR + "/" + default_variables['junos_parsers_dir'])
logger.debug('Importing junos parsers file: %s ',junos_parsers_yaml_files)
for junos_parsers_yaml_file in junos_parsers_yaml_files:
    try:
        with open(BASE_DIR + "/" + default_variables['junos_parsers_dir'] + "/"  + junos_parsers_yaml_file) as f:
            junos_parsers.append(yaml.load(f))
    except Exception, e:
        logger.error('Error importing junos parser: %s', junos_parsers_yaml_file)
 #       logging.exception(e)
        pass

pfe_parsers = []
pfe_parsers_yaml_files = os.listdir(BASE_DIR + "/" + default_variables['pfe_parsers_dir'])
logger.debug('Importing pfe parsers file: %s ',pfe_parsers_yaml_files)
for pfe_parsers_yaml_file in pfe_parsers_yaml_files:
    try:
        with open(BASE_DIR + "/" + default_variables['pfe_parsers_dir'] + "/" + pfe_parsers_yaml_file) as f:
            pfe_parsers.append(yaml.load(f))
    except Exception, e:
        logger.error('Error importing pfe parser: %s', pfe_parsers_yaml_file)
 #       logging.exception(e)
        pass

if __name__ == "__main__":


    logger.debug('Getting hosts that matches the specified tags')
    #  Get all hosts that matches with the tags
    target_hosts = get_target_hosts()
    logger.debug('The following hosts are being selected:  %s', target_hosts)

    # Test DB connectivity before starting to collect data
    if check_db_status():
        # Create a list of jobs and then iterate through
        # the number of threads appending each thread to
        # the job list
        target_hosts_lists = [target_hosts[x:x+len(target_hosts)/max_collector_threads+1] for x in range(0, len(target_hosts), len(target_hosts)/max_collector_threads+1)]

        jobs = []
        i=1
        for target_hosts_list in target_hosts_lists:
            logger.info('Collector Thread-%s scheduled with following hosts: %s', i, target_hosts_list)
            thread = threading.Thread(target=collector, kwargs={"host_list":target_hosts_list})
            jobs.append(thread)
            i=i+1

        # Start the threads
        for j in jobs:
            j.start()

        # Ensure all of the threads have finished
        for j in jobs:
            j.join()
