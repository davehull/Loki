#!/usr/bin/env python
# -*- coding: iso-8859-1 -*-
# -*- coding: utf-8 -*-
#
# Loki
# Simple IOC Scanner
#
# Detection is based on three detection methods:
#
# 1. File Name IOC
#    Applied to file names
#
# 2. Yara Check
#    Applied to files and processes
#
# 3. Hash Check
#    Compares known malicious hashes with th ones of the scanned files
#
# Loki combines all IOCs from ReginScanner and SkeletonKeyScanner and is the
# little brother of THOR our full-featured corporate APT Scanner
# 
# Florian Roth
# BSK Consulting GmbH
# 
# DISCLAIMER - USE AT YOUR OWN RISK.

import sys
import os
import argparse
import scandir
import traceback
import yara
import hashlib
import re
import stat
import datetime
import platform
import psutil
import binascii
import pylzma
import zlib
import struct
from StringIO import StringIO
from sets import Set
from colorama import Fore, Back, Style
from colorama import init

# Win32 Imports
try:
    import wmi
    import win32api
    from win32com.shell import shell
    isLinux = False
except Exception, e:
    print "Linux System - deactivating process memory check ..."
    isLinux= True

class Loki():

    # Signatures
    yara_rules = []
    filename_iocs = {}
    filename_ioc_desc = {}
    hashes = {}
    false_hashes = {}

    # Predefined paths to skip (Linux platform)
    LINUX_PATH_SKIPS_START = Set(["/proc", "/dev", "/media", "/sys/kernel/debug", "/sys/kernel/slab", "/sys/devices", "/usr/src/linux" ])
    LINUX_PATH_SKIPS_END = Set(["/initctl"])

    def __init__(self):

        # Set IOC path
        self.ioc_path = os.path.join(getApplicationPath(), "./iocs/")

        # Read IOCs -------------------------------------------------------
        # File Name IOCs (all files in iocs that contain 'filename')
        self.getFileNameIOCs(self.ioc_path)
        logger.log("INFO","File Name Characteristics initialized with %s regex patterns" % len(self.filename_iocs.keys()))

        # Hash based IOCs (all files in iocs that contain 'hash')
        self.getHashes(self.ioc_path)
        logger.log("INFO","Malware Hashes initialized with %s hashes" % len(self.hashes.keys()))

        # Hash based False Positives (all files in iocs that contain 'hash' and 'falsepositive')
        self.getHashes(self.ioc_path, false_positive=True)
        logger.log("INFO","False Positive Hashes initialized with %s hashes" % len(self.false_hashes.keys()))

        # Compile Yara Rules
        self.initializeYaraRules()


    def scanPath(self, path):

        # Startup
        logger.log("INFO","Scanning %s ...  " % path)

        # Counter
        c = 0

        # Get application path
        appPath = getApplicationPath()

        # Linux excludes from mtab
        if isLinux:
            allExcludes = self.LINUX_PATH_SKIPS_START | Set(getExcludedMountpoints())

        for root, directories, files in scandir.walk(path, onerror=walkError, followlinks=False):

                if isLinux:
                    # Skip paths that start with ..
                    newDirectories = []
                    for dir in directories:
                        skipIt = False
                        completePath = os.path.join(root, dir)
                        for skip in allExcludes:
                            if completePath.startswith(skip):
                                logger.log("INFO", "Skipping %s directory" % skip)
                                skipIt = True
                        if not skipIt:
                            newDirectories.append(dir)
                    directories[:] = newDirectories

                # Loop through files
                for filename in files:
                    try:

                        # Get the file and path
                        filePath = os.path.join(root,filename)

                        # Get Extension
                        extension = os.path.splitext(filePath)[1].lower()

                        # Linux directory skip
                        if isLinux:

                            # Skip paths that end with ..
                            for skip in self.LINUX_PATH_SKIPS_END:
                                if filePath.endswith(skip):
                                    if self.LINUX_PATH_SKIPS_END[skip] == 0:
                                        logger.log("INFO", "Skipping %s element" % skip)
                                        self.LINUX_PATH_SKIPS_END[skip] = 1

                            # File mode
                            mode = os.stat(filePath).st_mode
                            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISLNK(mode) or stat.S_ISSOCK(mode):
                                continue

                        # Counter
                        c += 1

                        if not args.noindicator:
                            printProgress(c)

                        # Skip program directory
                        # print appPath.lower() +" - "+ filePath.lower()
                        if appPath.lower() in filePath.lower():
                            logger.log("DEBUG", "Skipping file in program directory FILE: %s" % filePath)
                            continue

                        fileSize = os.stat(filePath).st_size
                        # print file_size

                        # File Name Checks -------------------------------------------------
                        for regex in self.filename_iocs.keys():
                            match = re.search(r'%s' % regex, filePath)
                            if match:
                                description = self.filename_ioc_desc[regex]
                                score = self.filename_iocs[regex]
                                if score > 70:
                                    logger.log("ALERT", "File Name IOC matched PATTERN: %s DESC: %s MATCH: %s" % (regex, description, filePath))
                                elif score > 40:
                                    logger.log("WARNING", "File Name Suspicious IOC matched PATTERN: %s DESC: %s MATCH: %s" % (regex, description, filePath))

                        # Access check (also used for magic header detection)
                        firstBytes = ""
                        try:
                            with open(filePath, 'rb') as f:
                                firstBytes = f.read(4)
                        except Exception, e:
                            logger.log("DEBUG", "Cannot open file %s (access denied)" % filePath)

                        # Evaluate Type
                        fileType = ""
                        if firstBytes.startswith("\x4d\x5a"):
                            fileType = "EXE"
                        if firstBytes.startswith("\x4d\x44\x4d\x50"):
                            fileType = "MDMP"
                        if firstBytes.startswith('CWS'):
                            fileType = "CWS"
                        if firstBytes.startswith('ZWS'):
                            fileType = "ZWS"

                        # Set fileData to an empty value
                        fileData = ""

                        # Evaluations -------------------------------------------------------
                        # Evaluate size
                        do_intense_check = True
                        if fileSize > ( args.s * 1024):
                             # Print files
                            if args.printAll:
                                logger.log("INFO", "Checking %s" % filePath)
                            do_hash_check = False
                        else:
                            if args.printAll:
                                logger.log("INFO", "Scanning %s" % filePath)

                        # Some file types will force intense check
                        if fileType == "MDMP":
                            do_intense_check = True

                        # Hash Check -------------------------------------------------------
                        # Do the check
                        md5 = "-"
                        sha1 = "-"
                        sha256 = "-"
                        if do_intense_check:

                            fileData = readFileData(filePath)
                            md5, sha1, sha256 = generateHashes(fileData)

                            logger.log("DEBUG", "MD5: %s SHA1: %s SHA256: %s FILE: %s" % ( md5, sha1, sha256, filePath ))

                            # False Positive Hash
                            if md5 in self.false_hashes.keys() or sha1 in self.false_hashes.keys() or sha256 in self.false_hashes.keys():
                                continue

                            # Malware Hash
                            matchType = None
                            matchDesc = None
                            matchHash = None
                            if md5 in self.hashes.keys():
                                matchType = "MD5"
                                matchDesc = self.hashes[md5]
                                matchHash = md5
                            elif sha1 in self.hashes.keys():
                                matchType = "SHA1"
                                matchDesc = self.hashes[sha1]
                                matchHash = sha1
                            elif sha256 in self.hashes.keys():
                                matchType = "SHA256"
                                matchDesc = self.hashes[sha256]
                                matchHash = sha256

                            # Hash string
                            hash_string = "MD5: %s SHA1: %s SHA256: %s" % ( md5, sha1, sha256 )

                            if matchType:
                                logger.log("ALERT", "Malware Hash TYPE: %s HASH: %s FILE: %s DESC: %s" % ( matchType, matchHash, filePath, matchDesc))

                        # Regin .EVT FS Check
                        if do_intense_check and len(fileData) > 11 and args.reginfs:

                            # Check if file is Regin virtual .evt file system
                            checkReginFS(fileData, filePath)

                        # Yara Check -------------------------------------------------------
                        # Size and type check
                        if do_intense_check:

                            # Read file data if hash check has been skipped
                            if not fileData:
                                fileData = readFileData(filePath)

                            # Memory Dump Scan
                            if fileType == "MDMP":
                                logger.log("INFO", "Scanning memory dump file %s" % filePath)

                            # Umcompressed SWF scan
                            if fileType == "ZWS" or fileType == "CWS":
                                logger.log("INFO", "Scanning decompressed SWF file %s" % filePath)
                                success, decompressedData = decompressSWFData(fileData)
                                if success:
                                   fileData = decompressedData

                            # Scan the read data
                            for (score, rule, description, matched_strings) in \
                                    self.scanData(fileData, fileType, filename, filePath, extension):

                                if score >= 70:
                                    logger.log("ALERT", "Yara Rule MATCH: %s DESCRIPTION: %s FILE: %s %s MATCHES: %s" % ( rule, description, filePath, hash_string, matched_strings))

                                elif score >= 40:
                                    logger.log("WARNING", "Yara Rule MATCH: %s DESCRIPTION: %s FILE: %s %s MATCHES: %s" % ( rule, description, filePath, hash_string, matched_strings))


                    except Exception, e:
                        if args.debug:
                            traceback.print_exc()


    def scanData(self, fileData, fileType="-", fileName="-", filePath="-", extension="-"):

        # Scan with yara
        try:
            for rules in self.yara_rules:

                # Yara Rule Match
                matches = rules.match(data=fileData,
                                      externals={
                                          'filename': fileName.lower(),
                                          'filepath': filePath.lower(),
                                          'extension': extension.lower(),
                                          'filetype': fileType.lower(),
                                      })

                # If matched
                if matches:
                    for match in matches:

                        score = 70
                        description = "not set"

                        # Built-in rules have meta fields (cannot be expected from custom rules)
                        if hasattr(match, 'meta'):

                            if 'description' in match.meta:
                                description = match.meta['description']

                            # If a score is given
                            if 'score' in match.meta:
                                score = int(match.meta['score'])

                        # Matching strings
                        matched_strings = ""
                        if hasattr(match, 'strings'):
                            # Get matching strings
                            matched_strings = getStringMatches(match.strings)

                        yield score, match.rule, description, matched_strings

        except Exception, e:
            if args.debug:
                traceback.print_exc()

    def scanProcesses(self):
        # WMI Handler
        c = wmi.WMI()
        processes = c.Win32_Process()
        t_systemroot = os.environ['SYSTEMROOT']

        # WinInit PID
        wininit_pid = 0
        # LSASS Counter
        lsass_count = 0

        for process in processes:

            try:

                # Gather Process Information --------------------------------------
                pid = process.ProcessId
                name = process.Name
                cmd = process.CommandLine
                if not cmd:
                    cmd = "N/A"
                if not name:
                    name = "N/A"
                path = "none"
                parent_pid = process.ParentProcessId
                priority = process.Priority
                ws_size = process.VirtualSize
                if process.ExecutablePath:
                    path = process.ExecutablePath
                # Owner
                try:
                    owner_raw = process.GetOwner()
                    owner = owner_raw[2]
                except Exception, e:
                    owner = "unknown"
                if not owner:
                    owner = "unknown"

            except Exception, e:
                logger.log("ALERT", "Error getting all process information. Did you run the scanner 'As Administrator'?")
                continue

            # Is parent to other processes - save PID
            if name == "wininit.exe":
                wininit_pid = pid

            # Skip some PIDs ------------------------------------------------------
            if pid == 0 or pid == 4:
                logger.log("INFO", "Skipping Process - PID: %s NAME: %s CMD: %s" % ( pid, name, cmd ))
                continue

            # Skip own process ----------------------------------------------------
            if os.getpid() == pid:
                logger.log("INFO", "Skipping LOKI Process - PID: %s NAME: %s CMD: %s" % ( pid, name, cmd ))
                continue

            # Print info ----------------------------------------------------------
            logger.log("NOTICE", "Scanning Process - PID: %s NAME: %s CMD: %s" % ( pid, name, cmd ))

            # Special Checks ------------------------------------------------------
            # better executable path
            if not "\\" in cmd and path != "none":
                cmd = path

            # Skeleton Key Malware Process
            if re.search(r'psexec .* [a-fA-F0-9]{32}', cmd, re.IGNORECASE):
                logger.log("WARNING", "Process that looks liks SKELETON KEY psexec execution detected PID: %s NAME: %s CMD: %s" % ( pid, name, cmd))

            # File Name Checks -------------------------------------------------
            for regex in self.filename_iocs.keys():
                match = re.search(r'%s' % regex, cmd)
                if match:
                    description = self.filename_ioc_desc[regex]
                    score = self.filename_iocs[regex]
                    if score > 70:
                        logger.log("ALERT", "File Name IOC matched PATTERN: %s DESC: %s MATCH: %s" % (regex, description, cmd))
                    elif score > 40:
                        logger.log("WARNING", "File Name Suspicious IOC matched PATTERN: %s DESC: %s MATCH: %s" % (regex, description, cmd))

            # Yara rule match
            # only on processes with a small working set size
            if int(ws_size) < ( 100 * 1048576 ): # 100 MB
                try:
                    alerts = []
                    for rules in self.yara_rules:
                        matches = rules.match(pid=pid)
                        if matches:
                            for match in matches:

                                # Preset memory_rule
                                memory_rule = 1

                                # Built-in rules have meta fields (cannot be expected from custom rules)
                                if hasattr(match, 'meta'):

                                    # If a score is given
                                    if 'memory' in match.meta:
                                        memory_rule = int(match.meta['memory'])

                                # If rule is meant to be applied to process memory as well
                                if memory_rule == 1:

                                    # print match.rule
                                    alerts.append("Yara Rule MATCH: %s PID: %s NAME: %s CMD: %s" % ( match.rule, pid, name, cmd))

                    if len(alerts) > 3:
                        logger.log("INFO", "Too many matches on process memory - most likely a false positive PID: %s NAME: %s CMD: %s" % (pid, name, cmd))
                    elif len(alerts) > 0:
                        for alert in alerts:
                            logger.log("ALERT", alert)
                except Exception, e:
                    logger.log("ERROR", "Error while process memory Yara check (maybe the process doesn't exist anymore or access denied). PID: %s NAME: %s" % ( pid, name))
            else:
                logger.log("DEBUG", "Skipped Yara memory check due to the process' big working set size (stability issues) PID: %s NAME: %s SIZE: %s" % ( pid, name, ws_size))

            ###############################################################
            # THOR Process Anomaly Checks
            # Source: Sysforensics http://goo.gl/P99QZQ

            # Process: System
            if name == "System" and not pid == 4:
                logger.log("WARNING", "System process without PID=4 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))

            # Process: smss.exe
            if name == "smss.exe" and not parent_pid == 4:
                logger.log("WARNING", "smss.exe parent PID is != 4 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if path != "none":
                if name == "smss.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "smss.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "smss.exe" and priority is not 11:
                logger.log("WARNING", "smss.exe priority is not 11 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))

            # Process: csrss.exe
            if path != "none":
                if name == "csrss.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "csrss.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "csrss.exe" and priority is not 13:
                logger.log("WARNING", "csrss.exe priority is not 13 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))

            # Process: wininit.exe
            if path != "none":
                if name == "wininit.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "wininit.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "wininit.exe" and priority is not 13:
                logger.log("NOTICE", "wininit.exe priority is not 13 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            # Is parent to other processes - save PID
            if name == "wininit.exe":
                wininit_pid = pid

            # Process: services.exe
            if path != "none":
                if name == "services.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "services.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "services.exe" and priority is not 9:
                logger.log("WARNING", "services.exe priority is not 9 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if wininit_pid > 0:
                if name == "services.exe" and not parent_pid == wininit_pid:
                    logger.log("WARNING", "services.exe parent PID is not the one of wininit.exe PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))

            # Process: lsass.exe
            if path != "none":
                if name == "lsass.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "lsass.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "lsass.exe" and priority is not 9:
                logger.log("WARNING", "lsass.exe priority is not 9 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if wininit_pid > 0:
                if name == "lsass.exe" and not parent_pid == wininit_pid:
                    logger.log("WARNING", "lsass.exe parent PID is not the one of wininit.exe PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            # Only a single lsass process is valid - count occurrences
            if name == "lsass.exe":
                lsass_count += 1
                if lsass_count > 1:
                    logger.log("WARNING", "lsass.exe count is higher than 1 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))

            # Process: svchost.exe
            if path is not "none":
                if name == "svchost.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "svchost.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "svchost.exe" and priority is not 8:
                logger.log("NOTICE", "svchost.exe priority is not 8 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if name == "svchost.exe" and not ( owner.upper().startswith("NT ") or owner.upper().startswith("NET") or owner.upper().startswith("LO") or owner.upper().startswith("SYSTEM") ):
                logger.log("WARNING", "svchost.exe process owner is suspicious PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))

            if name == "svchost.exe" and not " -k " in cmd and cmd != "N/A":
                print cmd
                logger.log("WARNING", "svchost.exe process does not contain a -k in its command line PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))

            # Process: lsm.exe
            if path != "none":
                if name == "lsm.exe" and not ( "system32" in path.lower() or "system32" in cmd.lower() ):
                    logger.log("WARNING", "lsm.exe path is not System32 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "lsm.exe" and priority is not 8:
                logger.log("NOTICE", "lsm.exe priority is not 8 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if name == "lsm.exe" and not ( owner.startswith("NT ") or owner.startswith("LO") or owner.startswith("SYSTEM") ):
                logger.log("WARNING", "lsm.exe process owner is suspicious PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if wininit_pid > 0:
                if name == "lsm.exe" and not parent_pid == wininit_pid:
                    logger.log("WARNING", "lsm.exe parent PID is not the one of wininit.exe PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))

            # Process: winlogon.exe
            if name == "winlogon.exe" and priority is not 13:
                logger.log("WARNING", "winlogon.exe priority is not 13 PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                    str(pid), name, owner, cmd, path))
            if re.search("(Windows 7|Windows Vista)", getPlatformFull()):
                if name == "winlogon.exe" and parent_pid > 0:
                    for proc in processes:
                        if parent_pid == proc.ProcessId:
                            logger.log("WARNING", "winlogon.exe has a parent ID but should have none PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s PARENTPID: %s" % (
                                str(pid), name, owner, cmd, path, str(parent_pid)))

            # Process: explorer.exe
            if path != "none":
                if name == "explorer.exe" and not t_systemroot.lower() in path.lower():
                    logger.log("WARNING", "explorer.exe path is not %%SYSTEMROOT%% PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                        str(pid), name, owner, cmd, path))
            if name == "explorer.exe" and parent_pid > 0:
                for proc in processes:
                    if parent_pid == proc.ProcessId:
                        logger.log("NOTICE", "explorer.exe has a parent ID but should have none PID: %s NAME: %s OWNER: %s CMD: %s PATH: %s" % (
                            str(pid), name, owner, cmd, path))

    def getFileNameIOCs(self, ioc_directory):

        try:
            for ioc_filename in os.listdir(ioc_directory):
                if 'filename' in ioc_filename:
                    with open(os.path.join(ioc_directory, ioc_filename), 'r') as file:
                        lines = file.readlines()

                        # Last Comment Line
                        last_comment = ""

                        for line in lines:
                            try:
                                # Empty
                                if re.search(r'^[\s]*$', line):
                                    continue

                                # Comments
                                if re.search(r'^#', line):
                                    last_comment = line.lstrip("#").lstrip(" ").rstrip("\n")
                                    continue

                                # Elements with description
                                if ";" in line:
                                    row = line.split(';')
                                    regex   = row[0]
                                    score   = row[1].rstrip(" ").rstrip("\n")
                                    desc    = last_comment

                                    # Catch legacy lines
                                    if not score.isdigit():
                                        desc = score # score is description (old format)
                                        score = 80 # default value

                                # Elements without description
                                else:
                                    regex = line

                                # Create list elements
                                self.filename_iocs[regex] = score
                                self.filename_ioc_desc[regex] = desc

                            except Exception, e:
                                logger.log("ERROR", "Error reading line: %s" % line)

        except Exception, e:
            traceback.print_exc()
            logger.log("ERROR", "Error reading File IOC file: %s" % ioc_filename)

    def initializeYaraRules(self):

        yaraRules = []
        filename_dummy = ""
        filepath_dummy = ""
        extension_dummy = ""
        filetype_dummy = ""

        try:
            for root, directories, files in scandir.walk(os.path.join(getApplicationPath(), "./signatures"), onerror=walkError, followlinks=False):
                for file in files:
                    try:

                        # Full Path
                        yaraRuleFile = os.path.join(root, file)

                        # Skip hidden, backup or system related files
                        if file.startswith(".") or file.startswith("~") or file.startswith("_"):
                            continue

                        # Extension
                        extension = os.path.splitext(file)[1].lower()

                        # Encrypted
                        if extension == ".yar":
                            try:
                                compiledRules = yara.compile(yaraRuleFile, externals= {
                                                                  'filename': filename_dummy,
                                                                  'filepath': filepath_dummy,
                                                                  'extension': extension_dummy,
                                                                  'filetype': filetype_dummy
                                                              })
                                yaraRules.append(compiledRules)
                                logger.log("INFO", "Initialized Yara rules from %s" % file)
                            except Exception, e:
                                logger.log("ERROR", "Error in Yara file: %s" % file)
                                if args.debug:
                                    traceback.print_exc()

                    except Exception, e:
                        logger.log("ERROR", "Error reading signature file %s ERROR: %s" % yaraRuleFile)
                        if args.debug:
                            traceback.print_exc()

            self.yara_rules = yaraRules

        except Exception, e:
            logger.log("ERROR", "Error reading signature folder /signatures/")
            if args.debug:
                traceback.print_exc()

    def getHashes(self, ioc_directory, false_positive=False):

        try:
            for ioc_filename in os.listdir(ioc_directory):
                if 'hash' in ioc_filename:
                    if false_positive and 'falsepositive' not in ioc_filename:
                        continue
                    with open(os.path.join(ioc_directory, ioc_filename), 'r') as file:
                        lines = file.readlines()

                        for line in lines:
                            try:
                                if re.search(r'^#', line) or re.search(r'^[\s]*$', line):
                                    continue
                                row = line.split(';')
                                hash = row[0]
                                comment = row[1].rstrip(" ").rstrip("\n")
                                # Empty File Hash
                                if hash == "d41d8cd98f00b204e9800998ecf8427e" or \
                                   hash == "da39a3ee5e6b4b0d3255bfef95601890afd80709" or \
                                   hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
                                    continue
                                # Else - check which type it is
                                if len(hash) == 32 or len(hash) == 40 or len(hash) == 64:
                                    if false_positive:
                                        self.false_hashes[hash.lower()] = comment
                                    else:
                                        self.hashes[hash.lower()] = comment
                            except Exception,e:
                                logger.log("ERROR", "Cannot read line: %s" % line)

        except Exception, e:
            traceback.print_exc()
            logger.log("ERROR", "Error reading Hash file: %s" % ioc_filename)

# Logger Class -----------------------------------------------------------------
class LokiLogger():

    no_log_file = False
    log_file = "loki.log"
    csv = False
    hostname = "NOTSET"
    alerts = 0
    warnings = 0
    only_relevant = False

    def __init__(self, no_log_file, log_file, hostname, csv, only_relevant):
        self.no_log_file = no_log_file
        self.log_file = log_file
        self.hostname = hostname
        self.csv = csv
        self.only_relevant = only_relevant

        # Welcome
        if not self.csv:
            self.print_welcome()

    def log(self, mes_type, message):

        if not args.debug and mes_type == "DEBUG":
            return

        # Counter
        if mes_type == "ALERT":
            self.alerts += 1
        if mes_type == "WARNING":
            self.warnings += 1

        if self.only_relevant:
            if mes_type not in ('ALERT', 'WARNING'):
                return

        # to stdout
        self.log_to_stdout(message, mes_type)

        # to file
        if not self.no_log_file:
            self.log_to_file(message, mes_type)

    def log_to_stdout(self, message, mes_type):

        # Prepare Message
        message = removeNonAsciiDrop(message)

        if self.csv:
            print "{0},{1},{2},{3}".format(getSyslogTimestamp(),self.hostname,mes_type,message)

        else:

            try:

                key_color = Fore.WHITE
                base_color = Fore.WHITE+Back.BLACK
                high_color = Fore.WHITE+Back.BLACK

                if mes_type == "NOTICE":
                    base_color = Fore.CYAN+''+Back.BLACK
                    high_color = Fore.BLACK+''+Back.CYAN
                elif mes_type == "INFO":
                    base_color = Fore.GREEN+''+Back.BLACK
                    high_color = Fore.BLACK+''+Back.GREEN
                elif mes_type == "WARNING":
                    base_color = Fore.YELLOW+''+Back.BLACK
                    high_color = Fore.BLACK+''+Back.YELLOW
                elif mes_type == "ALERT":
                    base_color = Fore.RED+''+Back.BLACK
                    high_color = Fore.BLACK+''+Back.RED
                elif mes_type == "DEBUG":
                    base_color = Fore.WHITE+''+Back.BLACK
                    high_color = Fore.BLACK+''+Back.WHITE
                elif mes_type == "ERROR":
                    base_color = Fore.MAGENTA+''+Back.BLACK
                    high_color = Fore.WHITE+''+Back.MAGENTA
                elif mes_type == "RESULT":
                    if "clean" in message.lower():
                        high_color = Fore.BLACK+Back.GREEN
                        base_color = Fore.GREEN+Back.BLACK
                    elif "suspicious" in message.lower():
                        high_color = Fore.BLACK+Back.YELLOW
                        base_color = Fore.YELLOW+Back.BLACK
                    else:
                        high_color = Fore.BLACK+Back.RED
                        base_color = Fore.RED+Back.BLACK

                # Colorize Type Word at the beginning of the line
                type_colorer = re.compile(r'([A-Z]{3,})', re.VERBOSE)
                mes_type = type_colorer.sub(high_color+r'[\1]'+base_color, mes_type)
                # Colorize Key Words
                colorer = re.compile('([A-Z_0-9]{2,}:)\s', re.VERBOSE)
                message = colorer.sub(key_color+Style.BRIGHT+r'\1 '+base_color+Style.NORMAL, message)
                # Break Line before REASONS
                linebreaker = re.compile('(MD5:|SHA1:|SHA256:|MATCHES:|FILE:)', re.VERBOSE)
                message = linebreaker.sub(r'\n\1', message)

                # Print to console
                if mes_type == "RESULT":
                    res_message = "\b\b%s %s" % (mes_type, message)
                    print base_color,res_message,Back.BLACK
                    print Fore.WHITE,Style.NORMAL
                else:
                    print base_color,"\b\b%s %s" % (mes_type, message),Back.BLACK,Fore.WHITE,Style.NORMAL

            except Exception, e:
                traceback.print_exc()
                print "Cannot print to cmd line - formatting error"

    def log_to_file(self, message, mes_type):
        try:
            # Write to file
            with open(self.log_file, "a") as logfile:
                if self.csv:
                    logfile.write("{0},{1},{2},{3}\n".format(getSyslogTimestamp(),self.hostname,mes_type,message))
                else:
                    logfile.write("%s %s LOKI: %s\n" % (getSyslogTimestamp(), self.hostname, message))
        except Exception, e:
            traceback.print_exc()
            print "Cannot print to log file {0}".format(self.log_file)

    def print_welcome(self):
        print Back.GREEN + " ".ljust(79) + Back.BLACK
        print "  "
        print "   " + Back.GREEN + "  " + Back.BLACK + "      " + Back.GREEN + "      " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK
        print "   " + Back.GREEN + "  " + Back.BLACK + "      " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "    " + Back.BLACK + "    " + Back.GREEN + "  " + Back.BLACK
        print "   " + Back.GREEN + "      " + Back.BLACK + "  " + Back.GREEN + "      " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK + "  " + Back.GREEN + "  " + Back.BLACK
        print "  "
        print "  Simple IOC Scanner"
        print "  "
        print "  (C) Florian Roth"
        print "  August 2015"
        print "  Version 0.9.2"
        print "  "
        print "  DISCLAIMER - USE AT YOUR OWN RISK"
        print "  "
        print Back.GREEN + " ".ljust(79) + Back.BLACK
        print Fore.WHITE+''+Back.BLACK

# Helper Functions -------------------------------------------------------------

def readFileData(filePath):
    fileData = ""
    try:
        # Read file complete
        with open(filePath, 'rb') as f:
            fileData = f.read()
    except Exception, e:
        logger.log("DEBUG", "Cannot open file %s (access denied)" % filePath)
    finally:
        return fileData

def generateHashes(filedata):
    try:
        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        sha256 = hashlib.sha256()
        md5.update(filedata)
        sha1.update(filedata)
        sha256.update(filedata)
        return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()
    except Exception, e:
        traceback.print_exc()
        return 0, 0, 0


def walkError(err):
    if "Error 3" in str(err):
        logger.log("ERROR", str(err))
    if args.debug:
        traceback.print_exc()


def removeNonAsciiDrop(string):
    nonascii = "error"
    #print "CON: ", string
    try:
        # Generate a new string without disturbing characters
        nonascii = "".join(i for i in string if ord(i)<127 and ord(i)>31)

    except Exception, e:
        traceback.print_exc()
        pass
    #print "NON: ", nonascii
    return nonascii


def getPlatformFull():
    type_info = ""
    try:
        type_info = "%s PROC: %s ARCH: %s" % ( " ".join(platform.win32_ver()), platform.processor(), " ".join(platform.architecture()))
    except Exception, e:
        type_info = " ".join(platform.win32_ver())
    return type_info


def setNice():
    try:
        pid = os.getpid()
        p = psutil.Process(pid)
        logger.log("INFO", "Setting LOKI process with PID: %s to priority IDLE" % pid)
        p.set_nice(psutil.IDLE_PRIORITY_CLASS)
        return 1
    except Exception, e:
        logger.log("ERROR", "Error setting nice value of THOR process")
        return 0


def getExcludedMountpoints():
    excludes = []
    mtab = open("/etc/mtab", "r")
    for mpoint in mtab:
        options = mpoint.split(" ")
        if not options[0].startswith("/dev/"):
            if not options[1] == "/":
                excludes.append(options[1])

    mtab.close()
    return excludes


def decompressSWFData(in_data):
    try:
        ver = in_data[3]

        if in_data[0] == 'C':
            # zlib SWF
            decompressData = zlib.decompress(in_data[8:])
        elif in_data[0] == 'Z':
            # lzma SWF
            decompressData = pylzma.decompress(in_data[12:])
        elif in_data[0] == 'F':
            # uncompressed SWF
            decompressData = in_data[8:]

        header = list(struct.unpack("<8B", in_data[0:8]))
        header[0] = ord('F')
        return True, struct.pack("<8B", *header) + decompressData

    except Exception, e:
        traceback.print_exc()
        return False, "Decompression error"


def getStringMatches(strings):
    try:
        string_matches = []
        matching_strings = ""
        for string in strings:
            # print string
            extract = string[2]
            if not extract in string_matches:
                string_matches.append(extract)

        string_num = 1
        for string in string_matches:
            matching_strings += " Str" + str(string_num) + ": " + removeNonAscii(removeBinaryZero(string))
            string_num += 1

        # Limit string
        if len(matching_strings) > 140:
            matching_strings = matching_strings[:140] + " ... (truncated)"

        return matching_strings.lstrip(" ")
    except:
        traceback.print_exc()


def checkReginFS(fileData, filePath):

    # Code section by Paul Rascagneres, G DATA Software
    # Adapted to work with the fileData already read to avoid
    # further disk I/O

    fp = StringIO(fileData)
    SectorSize=fp.read(2)[::-1]
    MaxSectorCount=fp.read(2)[::-1]
    MaxFileCount=fp.read(2)[::-1]
    FileTagLength=fp.read(1)[::-1]
    CRC32custom=fp.read(4)[::-1]

    # original code:
    # fp.close()
    # fp = open(filePath, 'r')

    # replaced with the following:
    fp.seek(0)

    data=fp.read(0x7)
    crc = binascii.crc32(data, 0x45)
    crc2 = '%08x' % (crc & 0xffffffff)

    logger.log("DEBUG", "Regin FS Check CRC2: %s" % crc2.encode('hex'))

    if CRC32custom.encode('hex') == crc2:
        logger.log("ALERT", "Regin Virtual Filesystem MATCH: %s" % filePath)


def removeBinaryZero(string):
    return re.sub(r'\x00','',string)


def printProgress(i):
    if (i%4) == 0:
        sys.stdout.write('\b/')
    elif (i%4) == 1:
        sys.stdout.write('\b-')
    elif (i%4) == 2:
        sys.stdout.write('\b\\')
    elif (i%4) == 3:
        sys.stdout.write('\b|')
    sys.stdout.flush()


def getApplicationPath():
    try:
        application_path = ""
        if getattr(sys, 'frozen', False):
            application_path = os.path.dirname(os.path.realpath(sys.executable))
        elif __file__:
            application_path = os.path.dirname(__file__)
        if application_path != "":
            # Working directory change skipped due to the function to create TXT, CSV and HTML file on the local file
            # system when thor is started from a read only network share
            # os.chdir(application_path)
            pass
        if application_path == "":
            application_path = os.path.dirname(os.path.realpath(__file__))
        if "~" in application_path and not isLinux:
            # print "Trying to translate"
            # print application_path
            application_path = win32api.GetLongPathName(application_path)
        #if args.debug:
        #    logger.log("DEBUG", "Application Path: %s" % application_path)
        return application_path
    except Exception, e:
        logger.log("ERROR","Error while evaluation of application path")


def removeNonAscii(string, stripit=False):
    nonascii = "error"

    try:
        try:
            # Handle according to the type
            if isinstance(string, unicode) and not stripit:
                nonascii = string.encode('unicode-escape')
            elif isinstance(string, str) and not stripit:
                nonascii = string.decode('utf-8', 'replace').encode('unicode-escape')
            else:
                try:
                    nonascii = string.encode('raw_unicode_escape')
                except Exception, e:
                    nonascii = str("%s" % string)

        except Exception, e:
            # traceback.print_exc()
            # print "All methods failed - removing characters"
            # Generate a new string without disturbing characters
            nonascii = "".join(i for i in string if ord(i)<127 and ord(i)>31)

    except Exception, e:
        traceback.print_exc()
        pass

    return nonascii


def getSyslogTimestamp():
    date_obj = datetime.datetime.utcnow()
    date_str = date_obj.strftime("%Y%m%dT%H:%M:%SZ")
    return date_str


# MAIN ################################################################
if __name__ == '__main__':

    # Parse Arguments
    parser = argparse.ArgumentParser(description='Loki - Simple IOC Scanner')
    parser.add_argument('-p', help='Path to scan', metavar='path', default='C:\\')
    parser.add_argument('-s', help='Maximum file site to check in KB (default 2000 KB)', metavar='kilobyte', default=2048)
    parser.add_argument('-l', help='Log file', metavar='log-file', default='loki.log')
    parser.add_argument('--printAll', action='store_true', help='Print all files that are scanned', default=False)
    parser.add_argument('--noprocscan', action='store_true', help='Skip the process scan', default=False)
    parser.add_argument('--nofilescan', action='store_true', help='Skip the file scan', default=False)
    parser.add_argument('--noindicator', action='store_true', help='Do not show a progress indicator', default=False)
    parser.add_argument('--reginfs', action='store_true', help='Do check for Regin virtual file system', default=False)
    parser.add_argument('--dontwait', action='store_true', help='Do not wait on exit', default=False)
    parser.add_argument('--csv', action='store_true', help='Write CSV log format to STDOUT (machine prcoessing)', default=False)
    parser.add_argument('--onlyrelevant', action='store_true', help='Only print warnings or alerts', default=False)
    parser.add_argument('--nolog', action='store_true', help='Don\'t write a local log file', default=False)
    parser.add_argument('--debug', action='store_true', default=False, help='Debug output')

    args = parser.parse_args()

    # Colorization ----------------------------------------------------
    init()

    # Remove old log file
    if os.path.exists(args.l):
        os.remove(args.l)

    # Computername
    if not isLinux:
        t_hostname = os.environ['COMPUTERNAME']
    else:
        t_hostname = os.uname()[1]

    # Logger
    logger = LokiLogger(args.nolog, args.l, t_hostname, args.csv, args.onlyrelevant)
    logger.log("INFO", "LOKI - Starting Loki Scan on %s" % t_hostname)

    # Loki
    loki = Loki()

    # Check if admin
    isAdmin = False
    if not isLinux:
        if shell.IsUserAnAdmin():
            isAdmin = True
            logger.log("INFO", "Current user has admin rights - very good")
        else:
            logger.log("NOTICE", "Program should be run 'as Administrator' to ensure all access rights to process memory and file objects.")
    else:
        if os.geteuid() == 0:
            isAdmin = True
            logger.log("INFO", "Current user is root - very good")
        else:
            logger.log("NOTICE", "Program should be run as 'root' to ensure all access rights to process memory and file objects.")

    # Set process to nice priority ------------------------------------
    if not isLinux:
        setNice()

    # Scan Processes --------------------------------------------------
    resultProc = False
    if not args.noprocscan and not isLinux:
        if isAdmin:
            loki.scanProcesses()
        else:
            logger.log("NOTICE", "Skipping process memory check. User has no admin rights.")

    # Scan Path -------------------------------------------------------
    # Set default
    defaultPath = args.p
    if isLinux and defaultPath == "C:\\":
        defaultPath = "/"

    resultFS = False
    if not args.nofilescan:
        loki.scanPath(defaultPath)

    # Result ----------------------------------------------------------
    if logger.alerts:
        logger.log("RESULT", "Indicators detected!")
        logger.log("RESULT", "Loki recommends a forensic analysis and triage with a professional triage tool like THOR APT Scanner.")
    elif logger.warnings:
        logger.log("RESULT", "Suspicious objects detected!")
        logger.log("RESULT", "Loki recommends a deeper analysis of the suspicious objects.")
    else:
        logger.log("RESULT", "SYSTEM SEEMS TO BE CLEAN.")

    if not args.dontwait:
        print " "
        raw_input("Press Enter to exit ...")