# Copyright (C) 2012-2013 Claudio Guarnieri.
# Copyright (C) 2014-2017 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import gridfs
import os
import logging

from cuckoo.common.abstracts import Report
from cuckoo.common.exceptions import CuckooReportError
from cuckoo.common.mongo import mongo
from cuckoo.common.objects import File

log = logging.getLogger(__name__)

try:
    from pymongo.errors import ConnectionFailure, InvalidDocument
    HAVE_MONGO = True
except ImportError:
    HAVE_MONGO = False

MONGOSIZELIMIT = 0x1000000
MEGABYTE = 0x100000

class MongoDB(Report):
    """Stores report in MongoDB."""
    order = 2

    # Mongo schema version, used for data migration.
    SCHEMA_VERSION = "1"

    db = None
    fs = None

    @classmethod
    def init_once(cls):
        if not mongo.init():
            return

        mongo.connect()
        cls.db = mongo.db
        cls.fs = mongo.grid

        # Set MongoDB schema version.
        if "cuckoo_schema" in mongo.collection_names:
            version = mongo.db.cuckoo_schema.find_one()["version"]
            if version != cls.SCHEMA_VERSION:
                raise CuckooReportError(
                    "Unknown MongoDB version schema version found. Cuckoo "
                    "doesn't really know how to proceed now.."
                )
        else:
            mongo.db.cuckoo_schema.save({"version": cls.SCHEMA_VERSION})

        # Set an unique index on stored files to avoid duplicates. As per the
        # pymongo documentation this is basically a no-op if the index already
        # exists. So we don't have to do that check ourselves.
        mongo.db.fs.files.ensure_index(
            "sha256", unique=True, sparse=True, name="sha256_unique"
        )

    def store_file(self, file_obj, filename=""):
        """Store a file in GridFS.
        @param file_obj: object to the file to store
        @param filename: name of the file to store
        @return: object id of the stored file
        """
        if not filename:
            filename = file_obj.get_name()

        existing = self.db.fs.files.find_one({"sha256": file_obj.get_sha256()})

        if existing:
            return existing["_id"]

        new = self.fs.new_file(filename=filename,
                               contentType=file_obj.get_content_type(),
                               sha256=file_obj.get_sha256())

        for chunk in file_obj.get_chunks():
            new.write(chunk)

        try:
            new.close()
            return new._id
        except gridfs.errors.FileExists:
            to_find = {"sha256": file_obj.get_sha256()}
            return self.db.fs.files.find_one(to_find)["_id"]

    def debug_dict_size(self, dct):
        if type(dct) == list:
            dct = dct[0]

        totals = dict((k, 0) for k in dct)
        def walk(root, key, val):
            if isinstance(val, dict):
                for k, v in val.iteritems():
                    walk(root, k, v)

            elif isinstance(val, (list, tuple, set)):
                for el in val:
                    walk(root, None, el)

            elif isinstance(val, basestring):
                totals[root] += len(val)

        for key, val in dct.iteritems():
            walk(key, key, val)

        return sorted(totals.items(), key=lambda item: item[1], reverse=True)

    def convertdict2unicode(self, mydict):
        newDict = {}
        for k, v in mydict.iteritems():
            if isinstance(v, str):
                newDict[k] = unicode(v, errors = 'replace')
            elif isinstance(v, list):
                newDict[k] = self.convert2unicode(v)
            elif isinstance(v, dict):
                newDict[k] = self.convertdict2unicode(v)
            else:
                newDict[k] = v
        return newDict;

    def convert2unicode(self, mylist):
        newList = []
        for v in mylist:
            if isinstance(v, str):
                newList.append( unicode(v, errors = 'replace') )
            elif isinstance(v, list):
                newList.append(self.convert2unicode(v))
            elif isinstance(v, dict):
                newList.append(self.convertdict2unicode(v))
            else:
                newList.append(v)
        return newList

    def run(self, results):
        """Writes report.
        @param results: analysis results dictionary.
        @raise CuckooReportError: if fails to connect or write to MongoDB.
        """
        # Create a copy of the dictionary. This is done in order to not modify
        # the original dictionary and possibly compromise the following
        # reporting modules.
        report = dict(results)
        if "network" not in report:
            report["network"] = {}

        # This will likely hardcode the cuckoo.log to this point, but that
        # should be fine.
        if report.get("debug"):
            report["debug"]["cuckoo"] = list(report["debug"]["cuckoo"])

        # Store path of the analysis path.
        report["info"]["analysis_path"] = self.analysis_path

        # Store the sample in GridFS.
        if results.get("info", {}).get("category") == "file" and "target" in results:
            sample = File(self.file_path)
            if sample.valid():
                fname = results["target"]["file"]["name"]
                sample_id = self.store_file(sample, filename=fname)
                report["target"] = {"file_id": sample_id}
                report["target"].update(results["target"])

        # Store the PCAP file in GridFS and reference it back in the report.
        pcap_path = os.path.join(self.analysis_path, "dump.pcap")
        pcap = File(pcap_path)
        if pcap.valid():
            pcap_id = self.store_file(pcap)
            report["network"]["pcap_id"] = pcap_id

        sorted_pcap_path = os.path.join(self.analysis_path, "dump_sorted.pcap")
        spcap = File(sorted_pcap_path)
        if spcap.valid():
            spcap_id = self.store_file(spcap)
            report["network"]["sorted_pcap_id"] = spcap_id

        mitmproxy_path = os.path.join(self.analysis_path, "dump.mitm")
        mitmpr = File(mitmproxy_path)
        if mitmpr.valid():
            mitmpr_id = self.store_file(mitmpr)
            report["network"]["mitmproxy_id"] = mitmpr_id

        # Store the process memory dump file and extracted files in GridFS and
        # reference it back in the report.
        if "procmemory" in report and self.options.get("store_memdump", False):
            for idx, procmem in enumerate(report["procmemory"]):
                procmem_path = os.path.join(
                    self.analysis_path, "memory", "%s.dmp" % procmem["pid"]
                )
                procmem_file = File(procmem_path)
                if procmem_file.valid():
                    procmem_id = self.store_file(procmem_file)
                    procmem["procmem_id"] = procmem_id

                for extracted in procmem.get("extracted", []):
                    f = File(extracted["path"])
                    if f.valid():
                        extracted["extracted_id"] = self.store_file(f)

        # Walk through the dropped files, store them in GridFS and update the
        # report with the ObjectIds.
        new_dropped = []
        if "dropped" in report:
            for dropped in report["dropped"]:
                new_drop = dict(dropped)
                drop = File(dropped["path"])
                if drop.valid():
                    dropped_id = self.store_file(drop, filename=dropped["name"])
                    new_drop["object_id"] = dropped_id

                new_dropped.append(new_drop)

        report["dropped"] = new_dropped

        new_extracted = []
        if "extracted" in report:
            for extracted in report["extracted"]:
                new_extr = dict(extracted)
                extr = File(extracted["raw"])
                if extr.valid():
                    extr_id = self.store_file(extr)
                    new_extr["object_id"] = extr_id

                new_extracted.append(new_extr)

        report["extracted"] = new_extracted

        # Add screenshots.
        report["shots"] = []
        if os.path.exists(self.shots_path):
            # Walk through the files and select the JPGs.
            for shot_file in sorted(os.listdir(self.shots_path)):
                if not shot_file.endswith(".jpg") or "_" in shot_file:
                    continue

                shot_path = os.path.join(self.shots_path, shot_file)
                shot_path_dir = os.path.dirname(shot_path)
                shot_file_name, shot_file_ext = os.path.splitext(shot_file)
                shot_path_resized = os.path.join(shot_path_dir, "%s_small%s" % (shot_file_name, shot_file_ext))

                shot_blob = {}

                # If the screenshot path is a valid file, store it and
                # reference it back in the report.
                if os.path.isfile(shot_path):
                    shot = File(shot_path)
                    if shot.valid():
                        shot_id = self.store_file(shot)
                        shot_blob["original"] = shot_id

                # Try to get the alternative (small) size for this image,
                # store it and reference it back in the report.
                if os.path.isfile(shot_path_resized):
                    shot_small = File(shot_path_resized)
                    if shot_small.valid():
                        shot_id = self.store_file(shot_small)
                        shot_blob["small"] = shot_id

                if shot_blob:
                    report["shots"].append(shot_blob)

        paginate = self.options.get("paginate", 100)

        # Store chunks of API calls in a different collection and reference
        # those chunks back in the report. In this way we should defeat the
        # issue with the oversized reports exceeding MongoDB's boundaries.
        # Also allows paging of the reports.
        if "behavior" in report and "processes" in report["behavior"]:
            new_processes = []
            for process in report["behavior"]["processes"]:
                new_process = dict(process)

                chunk = []
                chunks_ids = []
                # Loop on each process call.
                for call in process["calls"]:
                    # If the chunk size is paginate or if the loop is
                    # completed then store the chunk in MongoDB.
                    if len(chunk) == paginate:
                        to_insert = {"pid": process["pid"], "calls": self.convert2unicode(chunk)}
                        chunk_id = self.db.calls.insert(to_insert)
                        chunks_ids.append(chunk_id)
                        # Reset the chunk.
                        chunk = []

                    # Append call to the chunk.
                    chunk.append(call)

                # Store leftovers.
                if chunk:
                    to_insert = {"pid": process["pid"], "calls": self.convert2unicode(chunk)}
                    chunk_id = self.db.calls.insert(to_insert)
                    chunks_ids.append(chunk_id)

                # Add list of chunks.
                new_process["calls"] = chunks_ids
                new_processes.append(new_process)

            # Store the results in the report.
            report["behavior"] = dict(report["behavior"])
            report["behavior"]["processes"] = new_processes

        if report.get("procmon"):
            procmon, chunk = [], []

            for entry in report["procmon"]:
                if len(chunk) == paginate:
                    procmon.append(self.db.procmon.insert(chunk))
                    chunk = []

                chunk.append(entry)

            if chunk:
                procmon.append(self.db.procmon.insert(chunk))

            report["procmon"] = procmon

        try:
            # Store the report and retrieve its object id.
            self.db.analysis.save(report)
        except InvalidDocument as e:
            parent_key, psize = self.debug_dict_size(report)[0]
            if not self.options.get("fix_large_docs", False):
                # Just log the error and problem keys
                log.info(str(e))
                log.info("Largest parent key: %s (%d MB)" % (parent_key, int(psize) / MEGABYTE))
            else:
                # Delete the problem keys and check for more
                error_saved = True
                size_filter = MONGOSIZELIMIT
                while error_saved:
                    if type(report) == list:
                        report = report[0]
                    try:
                        if type(report[parent_key]) == list:
                            for j, parent_dict in enumerate(report[parent_key]):
                                child_key, csize = self.debug_dict_size(parent_dict)[0]
                                if csize > size_filter:
                                    log.warn("results['%s']['%s'] deleted due to size: %s" % (parent_key, child_key, csize))
                                    del report[parent_key][j][child_key]
                        else:
                            child_key, csize = self.debug_dict_size(report[parent_key])[0]
                            if csize > size_filter:
                                log.warn("results['%s']['%s'] deleted due to size: %s" % (parent_key, child_key, csize))
                                del report[parent_key][child_key]
                        try:
                            self.db.analysis.save(report)
                            error_saved = False
                        except InvalidDocument as e:
                            parent_key, psize = self.debug_dict_size(report)[0]
                            log.info(str(e))
                            log.info("Largest parent key: %s (%d MB)" % (parent_key, int(psize) / MEGABYTE))
                            size_filter = size_filter - MEGABYTE
                    except Exception as e:
                        log.error("Failed to delete child key: %s" % str(e))
                        error_saved = False
