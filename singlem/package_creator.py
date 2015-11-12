from graftm.graftm_package import GraftMPackage, GraftMPackageVersion3
from skbio.tree import TreeNode
import logging
import tempfile
from Bio import SeqIO
import extern

class PackageCreator:
    def create(self, **kwargs):
        input_graftm_package_path = kwargs.pop('input_graftm_package')
        output_singlem_package_path = kwargs.pop('output_singlem_package')
        if len(kwargs) > 0:
            raise Exception("Unexpected arguments detected: %s" % kwargs)
        
        # Remove sequences from diamond database that are not in the tree
        gpkg = GraftMPackage.acquire(input_graftm_package_path)
        tree_leaves = set()
        for node in TreeNode.read(gpkg.reference_package_tree_path()).tips():
            if node.name in tree_leaves:
                raise Exception("Found duplicate tree leaf name in graftm package tree. Currently this case is not handled, sorry")
            tree_leaves.add(node.name)
        for name in tree_leaves: #I don't think there is a 'peek' ?
            eg_name = name
            break
        logging.info("Read in %i tree tip names e.g. %s" % (len(tree_leaves),
                                                            eg_name))
        
        # Make a new fasta file of all the sequences that are leaves
        found_sequence_names = set()
        num_seqs_unaligned = 0
        with tempfile.NamedTemporaryFile(prefix='singlem_package_creator',suffix='.fasta') as t:
            for s in SeqIO.parse(gpkg.unaligned_sequence_database_path(), "fasta"):
                num_seqs_unaligned += 1
                if s.id in tree_leaves:
                    if s.id in found_sequence_names:
                        raise Exception("Found duplicate sequence names in graftm unaligned sequence fasta file. Currently this case is not handled, sorry")
                    SeqIO.write([s], t, "fasta")
                    found_sequence_names.add(s.id)
            t.flush()
                    
            if len(tree_leaves) != len(found_sequence_names):
                raise Exception("Found some sequences that were in the tree but not the unaligned sequences database. Something is likely amiss with the input GraftM package")
            logging.info("All %i sequences found in tree extracted successfully from unaligned sequences fasta file, which originally had %i sequences" % (\
                len(found_sequence_names), num_seqs_unaligned))
            
            # Create a new diamond database
            with tempfile.NamedTemporaryFile(prefix='singlem_package_creator',suffix='.dmnd') as dmnd:
                cmd = "diamond makedb --in '%s' -d '%s'" % (t.name, dmnd.name)
                extern.run(cmd)
            
                # Compile the final graftm/singlem package
                if len(gpkg.search_hmm_paths()) == 1 and gpkg.search_hmm_paths()[0] == gpkg.alignment_hmm_path():
                    search_hmms = None
                else:
                    search_hmms = gpkg.search_hmm_paths()
                GraftMPackageVersion3.compile(output_singlem_package_path,
                                              gpkg.reference_package_path(),
                                              gpkg.alignment_hmm_path(),
                                              dmnd.name,
                                              gpkg.maximum_range(),
                                              t.name,
                                              gpkg.use_hmm_trusted_cutoff(),
                                              search_hmms)
                logging.info("SingleM-compatible package creation finished")
                