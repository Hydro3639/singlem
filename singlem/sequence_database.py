import csv
import os
import tempfile
import StringIO
import logging
import subprocess
import itertools
import sqlite3
import glob
import json

from itertools import izip_longest
from threading import Thread
from Bio import SeqIO
from orator import DatabaseManager, Model

from otu_table import OtuTableEntry, OtuTable
import extern

class DBSequence(OtuTableEntry):
    sequence_id = None

    def fasta_defline(self):
        '''return the name of the sequence with all the info encoded'''
        return "|".join([
            self.marker,
            self.sample_name,
            str(self.count),
            self.taxonomy])

    @staticmethod
    def parse_from_fasta_define(defline):
        '''The opposite of fasta_define(), parse info into instance variables in
        an array of DBSequence objects
        '''
        splits = defline.split(' ')
        if (splits) < 2: raise Exception("Parse exception: %s" % defline)
        sequence_id = int(splits[0].replace('lcl|',''))

        splits = ' '.join(splits[1:])
        to_return = []
        for subsplit in splits.split(SequenceDatabase.DEFLINE_DELIMITER_CHARACTER):
            splits2 = subsplit.split('|')
            if len(splits2) != 4: raise Exception("Parse exception 2: %s" % defline)
            dbseq = DBSequence()
            dbseq.sequence_id = sequence_id
            dbseq.marker,\
            dbseq.sample_name,\
            dbseq.count,\
            dbseq.taxonomy = splits2
            dbseq.count = int(dbseq.count)
            to_return.append(dbseq)
        return to_return

class SequenceDatabase:
    version = 3
    SQLITE_DB_NAME = 'otus.sqlite3'
    DEFAULT_CLUSTERING_DIVERGENCE = 3
    _marker_to_smafadb = {}

    _CONTENTS_FILE_NAME = 'CONTENTS.json'

    VERSION_KEY = 'singlem_database_version'
    SMAFA_CLUSTER_DIVERGENCE_KEY = 'smafa_clustering_divergence'

    _REQUIRED_KEYS = {3: [VERSION_KEY, SMAFA_CLUSTER_DIVERGENCE_KEY]}

    def add_smafa_db(self, marker_name, smafa_db):
        self._marker_to_smafadb[marker_name] = smafa_db

    def get_smafa_db(self, marker_name):
        if marker_name in self._marker_to_smafadb:
            return self._marker_to_smafadb[marker_name]
        else:
            logging.debug("No smafa DB found for %s" % marker_name)
            return None

    def smafa_dbs(self):
        return self._marker_to_smafadb.values()

    def smafa_clustering_divergence(self):
        return self._contents_hash[
            SequenceDatabase.SMAFA_CLUSTER_DIVERGENCE_KEY]

    @staticmethod
    def acquire(path):
        db = SequenceDatabase()

        contents_path = os.path.join(
            path, SequenceDatabase._CONTENTS_FILE_NAME)
        if not os.path.exists(contents_path):
            logging.error("The SingleM database located at '{}' did not contain a contents file.".format(
                path) +
                          "This means that the DB is not at that location, is corrupt, or was generated by SingleM version 0.11.0 or older. So unfortunately the DB could not be loaded.")
            raise Exception("Failed to find contents file in SingleM DB {}".format(
                contents_path))
        with open(contents_path) as f:
            db._contents_hash = json.load(f)

        found_version = db._contents_hash[SequenceDatabase.VERSION_KEY]
        logging.debug("Loading version {} SingleM database: {}".format(
            found_version, path))
        if found_version == 3:
            for key in SequenceDatabase._REQUIRED_KEYS[found_version]:
                if key not in db._contents_hash:
                    raise Exception(
                        "Unexpectedly did not find required key {} in SingleM database contents file: {}".format(
                            key, path))
        else:
            raise Exception("Unexpected SingleM DB version found: {}".format(found_version))

        db.sqlite_file = os.path.join(path, SequenceDatabase.SQLITE_DB_NAME)
        smafas = glob.glob("%s/*.smafadb" % path)
        logging.debug("Found smafadbs: %s" % ", ".join(smafas))
        if len(smafas) == 0: raise Exception("No smafa DBs found in DB")
        for g in smafas:
            marker = os.path.basename(g).replace('.smafadb','')
            db.add_smafa_db(marker, g)
        return db

    @staticmethod
    def grouper(iterable, n):
        args = [iter(iterable)] * n
        return izip_longest(*args, fillvalue=None)

    @staticmethod
    def create_from_otu_table(db_path, otu_table_collection,
                              clustering_divergence=DEFAULT_CLUSTERING_DIVERGENCE):
        # ensure db does not already exist
        if os.path.exists(db_path):
            raise Exception("Cowardly refusing to overwrite already-existing database file '%s'" % db_path)
        logging.info("Creating SingleM database at {}".format(db_path))
        os.makedirs(db_path)

        # Create contents file
        contents_file_path = os.path.join(db_path, SequenceDatabase._CONTENTS_FILE_NAME)
        with open(contents_file_path, 'w') as f:
            json.dump({
                SequenceDatabase.VERSION_KEY: 3,
                SequenceDatabase.SMAFA_CLUSTER_DIVERGENCE_KEY: clustering_divergence
            }, f)

        # setup sqlite DB
        sqlite_db_path = os.path.join(db_path, SequenceDatabase.SQLITE_DB_NAME)
        logging.debug("Connecting to db %s" % sqlite_db_path)
        db = sqlite3.connect(sqlite_db_path)
        c = db.cursor()
        c.execute("CREATE TABLE otus (marker text, sample_name text,"
                  " sequence text, num_hits int, coverage float, taxonomy text)")
        c.execute("CREATE TABLE clusters (member text, representative text)")
        db.commit()

        gene_to_tempfile = {}

        chunksize = 10000 # Run in chunks for sqlite insert performance.
        for chunk in SequenceDatabase.grouper(otu_table_collection, chunksize):
            chunk_list = []
            for entry in chunk:
                if entry is not None: # Is None when padded in last chunk.
                    chunk_list.append((entry.marker, entry.sample_name, entry.sequence, entry.count,
                        entry.coverage, entry.taxonomy))
                    dbseq = DBSequence()
                    dbseq.marker = entry.marker
                    dbseq.sample_name = entry.sample_name
                    dbseq.sequence = entry.sequence
                    dbseq.count = entry.count
                    dbseq.taxonomy = entry.taxonomy

                    if entry.marker not in gene_to_tempfile:
                        gene_to_tempfile[entry.marker] = tempfile.NamedTemporaryFile(prefix='singlem-makedb')
                    tf = gene_to_tempfile[entry.marker]
                    tf.write("%s\n" % entry.sequence)

            c.executemany("INSERT INTO otus(marker, sample_name, sequence, num_hits, "
                          "coverage, taxonomy) VALUES(?,?,?,?,?,?)",
                          chunk_list)

        logging.info("Creating SQLite indices")
        c.execute("CREATE INDEX otu_sequence on otus (sequence)")
        c.execute("CREATE INDEX otu_sample_name on otus (sample_name)")
        db.commit()

        # Run smafa on each of the genomes
        logging.info("Running smafas")
        for marker_name, tf in gene_to_tempfile.items():
            smafa = "%s.smafadb" % os.path.join(db_path, marker_name)
            tf.flush()
            cmd = "sort -S20% '{}' |uniq |awk '{{print \">1\\n\" $1}}' |smafa cluster --fragment-method /dev/stdin --divergence {} |tee >(cut -f2 |sort -S20% |uniq |awk '{{print \">1\\n\" $1}}' |smafa makedb /dev/stdin '{}')" \
                .format(tf.name, clustering_divergence, smafa)
            logging.debug("Running cmd: %s", cmd)
            logging.info("Formatting smafa database %s .." % smafa)
            # Use streaming technique from
            # https://stackoverflow.com/questions/2715847/python-read-streaming-input-from-subprocess-communicate#17698359
            p = subprocess.Popen(['bash','-c',cmd], stdout=subprocess.PIPE, bufsize=1)
            with p.stdout:
                for line in iter(p.stdout.readline, b''):
                    splits = line.rstrip().split("\t")
                    if len(splits) != 2:
                        raise Exception("Unexpected smafa cluster output: {}".format(line))
                    c.execute("INSERT INTO clusters VALUES (?,?)",splits)
            p.wait()
            db.commit()
            tf.close()
        logging.info("Generating cluster SQL index ..")
        c.execute("CREATE INDEX clusters_representative on clusters(representative)")
        logging.info("Finished")

    @staticmethod
    def dump(db_path):
        """Dump the DB contents to STDOUT, requiring only that the DB is a version that
        has an otus table in sqlite3 form (i.e. version 2 and 3 at least).

        """
        sqlite_db = os.path.join(db_path, SequenceDatabase.SQLITE_DB_NAME)
        logging.debug("Connecting to DB {}".format(sqlite_db))
        if not os.path.exists(sqlite_db):
            raise Exception("SQLite3 database does not appear to exist in the SingleM database - perhaps it is the wrong version?")
        db = DatabaseManager({
        'sqlite3': {
            'driver': 'sqlite',
            'database': sqlite_db
        }})
        Model.set_connection_resolver(db)
        print "\t".join(OtuTable.DEFAULT_OUTPUT_FIELDS)
        for chunk in db.table('otus').chunk(1000):
            for entry in chunk:
                otu = OtuTableEntry()
                otu.marker = entry.marker
                otu.sample_name = entry.sample_name
                otu.sequence = entry.sequence
                otu.count = entry.num_hits
                otu.coverage = entry.coverage
                otu.taxonomy = entry.taxonomy
                print str(otu)
