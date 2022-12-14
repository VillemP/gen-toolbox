import argparse
import datetime
import glob
import os.path
import re
import shutil
import sys
from pathlib import Path
from sys import stderr

import hail as hl
from pyspark import *

import file_utility

hail_home = Path(hl.__file__).parent.__str__()
unique = hash(datetime.datetime.utcnow())
def vcfs_to_matrixtable(f, destination=None, write=True, annotate=True):
    files = list()
    if type(f) is list:
        for vcf in f:
            files.append(vcf)

    elif not f.endswith(".vcf") and not f.endswith(".gz"):
        with open(f) as vcflist:
            for vcfpath in vcflist:
                stripped = vcfpath.strip()
                assert os.path.exists(stripped)
                files.append(stripped)
    else:
        assert os.path.exists(f), "Path {0} does not exist.".format(f)
        files.append(f)  # Only one file

    # recode = {f"chr{i}":f"{i}" for i in (list(range(1, 23)) + ['X', 'Y'])}
    # Can import only samples of the same key (matrixtable join), if input is list of vcfs
    table = hl.import_vcf(files, force=True, reference_genome='GRCh37', contig_recoding={"chr1": "1",
                                                                                         "chr2": "2",
                                                                                         "chr3": "3",
                                                                                         "chr4": "4",
                                                                                         "chr5": "5",
                                                                                         "chr6": "6",
                                                                                         "chr7": "7",
                                                                                         "chr8": "8",
                                                                                         "chr9": "9",
                                                                                         "chr10": "10",
                                                                                         "chr11": "11",
                                                                                         "chr12": "12",
                                                                                         "chr13": "13",
                                                                                         "chr14": "14",
                                                                                         "chr15": "15",
                                                                                         "chr16": "16",
                                                                                         "chr17": "17",
                                                                                         "chr18": "18",
                                                                                         "chr19": "19",
                                                                                         "chr20": "20",
                                                                                         "chr21": "21",
                                                                                         "chr22": "22",
                                                                                         "chrX": "X",
                                                                                         "chrY": "Y"})
    if annotate:
        table = table.filter_rows(table.alleles[1] != '*')  # These alleles break VEP, filter out star alleles.
        table = hl.methods.vep(table, config="vep_settings.json", csq=True)
    if write:
        if not os.path.exists(destination):
            table.write(destination)
        else:
            raise FileExistsError(destination)
    return table


def parse_empty(text):
    return hl.if_else(text == "", hl.missing(hl.tint32), hl.float(text))


def parse_csq(vcf):
    return None

def append_table(table, prefix, out=None, write=False, metadata=None):
    # mt_a = table.annotate_rows(CSQ=table.info.CSQ.first().split("\\|"))
    # # mt_a = mt_a.drop(mt_a.info) # Drop the already split string
    # mt_a = mt_a.annotate_rows(impact=mt_a.CSQ[2])
    # mt_a = mt_a.annotate_rows(gene=mt_a.CSQ[3])
    # mt_a = mt_a.annotate_rows(Entrez_ID=hl.int(parse_empty(mt_a.CSQ[4])))
    # mt_a = mt_a.annotate_rows(AC=mt_a.info.AC)
    # mt_a = mt_a.annotate_rows(CADD_phred=hl.float(parse_empty(mt_a.CSQ[33])))
    # mt_a = mt_a.filter_entries((hl.len(mt_a.filters) == 0), keep=True)  # Remove all not PASS
    # mt_a = mt_a.annotate_rows(gnomAD_exomes_AF=hl.float(parse_empty(mt_a.CSQ[36])))
    # mt_a = mt_a.annotate_rows(MAX_AF=hl.float(parse_empty(mt_a.CSQ[40])))
    mt_a = table
    mt_a = mt_a.annotate_rows(VEP_str=mt_a.vep.first().split("\\|"))
    mt_a = mt_a.annotate_entries(AC=mt_a.GT.n_alt_alleles(),
                                 VF=hl.float(mt_a.AD[1]/mt_a.DP))
    mt_a = mt_a.annotate_rows(impact=mt_a.VEP_str[0],
                              gene=mt_a.VEP_str[1],
                              HGNC_ID=hl.int(parse_empty(mt_a.VEP_str[2])),
                              MAX_AF=hl.float(parse_empty(mt_a.VEP_str[3])))
    mt_a = mt_a.drop(mt_a.info)
    mt_a = mt_a.filter_entries(mt_a.VF>=0.3, keep=True)  # Remove all not ALT_pos/DP < 0.3
    if metadata is not None:
        phen, mut = metadata.get(prefix, ["NA","NA"])
        if len(phen) == 0: phen="NA"
        if len(mut) == 0: mut = "NA"
        mt_a = mt_a.annotate_globals(metadata=hl.struct(phenotype = phen, mutation=mut))

    if write and out is not None:
        mt_a.write(out)
    return mt_a


def parse_tables(tables):
    mt_tables = []
    # Positional arguments from VEP annotated CSQ string. TODO: Query from VCF header
    for i, table in enumerate(tables):
        mt_tables.append(append_table(table))

    return mt_tables


def mts_to_table(tables):
    for i, tb in enumerate(tables):
        tb = tb.key_cols_by()
        tb = tb.entries()  # Convert from MatrixTable to Table
        tables[i] = tb.key_by(tb.gene)  # Key by gene
    return tables


def mt_join(mt_list):
    mt_final = None
    for i, mt in enumerate(mt_list):
        if i == 0:
            mt_final = mt
        mt_final = hl.experimental.full_outer_join_mt(mt_final, mt)  # An outer join of MatrixTables
        # mt_final.write(mt_path)
    return mt_final


def table_join(tables_list):
    # Join Tables into one Table.
    if tables_list is not None and len(tables_list) > 0:
        unioned = tables_list[0]  # Initialize with a single table
    else:
        raise Exception("No tables to be joined based on current configuration.")
    if len(tables_list) > 1:
        unioned = unioned.union(*tables_list[1:], unify=True)
    return unioned.cache()


def get_metadata(metadata_path):
    p = Path(metadata_path)
    metadata_dict = dict()
    assert p.exists()
    with p.open(encoding="latin-1") as f:
        for line in f.readlines():
            s = line.strip().split("\t")
            ecode = file_utility.trim_prefix(s[0])
            if ecode not in metadata_dict:
                if len(s) >= 2:
                    metadata_dict[ecode] = [s[1], s[2]]
                else:
                    metadata_dict[ecode] = ["NA", "NA"]
            else:
                sys.stderr.write("Found duplicate key {0} for line {1}. Existing object {2}.\n"
                                 .format(ecode, s, (ecode, metadata_dict[ecode])))
    return metadata_dict


def gnomad_table(unioned, text="modifier"):
    sys.stderr.write("Creating MAX_AF_frequency table\n")
    gnomad_tb = unioned.group_by(unioned.gene).aggregate(
        modifier=hl.struct(
            gnomad_1=hl.agg.filter(
                (unioned.MAX_AF < 0.01) & (unioned.impact.contains(hl.literal("MODIFIER"))),
                hl.agg.sum(unioned.AC)),
            gnomad_1_5=hl.agg.filter((unioned.MAX_AF > 0.01) & (unioned.MAX_AF < 0.05) & (
                unioned.impact.contains(hl.literal("MODIFIER"))), hl.agg.sum(unioned.AC)),
            gnomad_5_100=hl.agg.filter((unioned.MAX_AF > 0.05) & (
                unioned.impact.contains(hl.literal("MODIFIER"))), hl.agg.sum(unioned.AC))),
        low=hl.struct(
            gnomad_1=hl.agg.filter(
                (unioned.MAX_AF < 0.01) & (unioned.impact.contains(hl.literal("LOW"))),
                hl.agg.sum(unioned.AC)),
            gnomad_1_5=hl.agg.filter((unioned.MAX_AF > 0.01) & (unioned.MAX_AF < 0.05) & (
                unioned.impact.contains(hl.literal("LOW"))), hl.agg.sum(unioned.AC)),
            gnomad_5_100=hl.agg.filter((unioned.MAX_AF > 0.05) & (
                unioned.impact.contains(hl.literal("LOW"))), hl.agg.sum(unioned.AC))),
        moderate=hl.struct(
            gnomad_1=hl.agg.filter(
                (unioned.MAX_AF < 0.01) & (unioned.impact.contains(hl.literal("MODERATE"))),
                hl.agg.sum(unioned.AC)),
            gnomad_1_5=hl.agg.filter((unioned.MAX_AF > 0.01) & (unioned.MAX_AF < 0.05) & (
                unioned.impact.contains(hl.literal("MODERATE"))), hl.agg.sum(unioned.AC)),
            gnomad_5_100=hl.agg.filter((unioned.MAX_AF > 0.05) & (
                unioned.impact.contains(hl.literal("MODERATE"))), hl.agg.sum(unioned.AC))),
        high=hl.struct(
            gnomad_1=hl.agg.filter(
                (unioned.MAX_AF < 0.01) & (unioned.impact.contains(hl.literal("HIGH"))),
                hl.agg.sum(unioned.AC)),
            gnomad_1_5=hl.agg.filter((unioned.MAX_AF > 0.01) & (unioned.MAX_AF < 0.05) & (
                unioned.impact.contains(hl.literal("HIGH"))), hl.agg.sum(unioned.AC)),
            gnomad_5_100=hl.agg.filter((unioned.MAX_AF > 0.05) & (
                unioned.impact.contains(hl.literal("HIGH"))), hl.agg.sum(unioned.AC)))
    )
    return gnomad_tb


def write_gnomad_table(vcfs, dest, overwrite=False, metadata=None):
    gnomad_tb = None
    hailtables = dict()
    metadata_dict = get_metadata(metadata)
    for vcfpath in vcfs:
        assert vcfpath.exists()
        prefix = file_utility.trim_prefix(vcfpath.stem)
        destination = Path(dest).joinpath(vcfpath.stem)
        if overwrite or not destination.exists():
            # Read all vcfs and make a dict, keeps in memory!
            hailtables[prefix] = append_table(vcfs_to_matrixtable(vcfpath.__str__(), destination.__str__(), False),
                                              prefix, destination.__str__(), True, metadata_dict)
        elif destination.exists():
            hailtables[prefix] = hl.read_matrix_table(destination.__str__())
            sys.stderr.write("Overwrite is not active, opening existing file instead: {0}\n"
                             .format(destination.__str__()))
        else:
            FileExistsError("The output HailTable exists and --overwrite is not active in destination {0}"
                            .format(destination.__str__()))


# Turn MatrixTables into HailTables, keyed by gene, join
    unioned_table = table_join(mts_to_table(list(hailtables.values())))

    gnomad_tb = gnomad_table(unioned_table)
    gnomadpath = Path(dest).joinpath(Path("gnomad_tb"))
    if gnomadpath.exists():
        if not overwrite:
            raise FileExistsError(gnomadpath)
        else:
            stderr.write("WARNING: Overwrite is active. Deleting pre-existing directory {0}\n".format(gnomadpath))
            shutil.rmtree(gnomadpath)
    else:
        gnomad_tb.write(gnomadpath.__str__())
        pass
    return gnomad_tb

def _not(func):
    """
    https://stackoverflow.com/questions/33989155/is-there-a-filter-opposite-builtin
    :param func:
    :return:
    """
    def not_func(*args, **kwargs):
        return not func(*args, **kwargs)
    return not_func
def load_hailtables(dest, number, out=None, metadata=None, overwrite=False, phenotype=None):
    hailtables = dict()
    gnomadpath = Path(dest).joinpath(Path("gnomad_tb", str(unique)))
    ### TODO: Remove temporary fix
    gnomad_tb = None
    ecode_phenotype = dict()
    inverse_matches = dict()
    ###
    count = sum(1 for t in dest.iterdir())
    sys.stderr.write("{0} items in folder {1}\n".format(count, str(dest)))
    i = 0
    toolbar_width = 1 if count//10 == 0 else count//10
    # setup toolbar
    sys.stderr.write("Loading MatrixTables\n Progress\n")

    for idx, folder in enumerate(dest.iterdir(), 1):
        if folder.is_dir():
            vcfname = folder.name
            outpath = Path(out).joinpath(vcfname)
            #print(vcfname)
            if vcfname.rfind("gnomad_tb") == -1:  # Skip the folders containing the end product
                prefix = file_utility.trim_prefix(vcfname)
                mt_a = hl.read_matrix_table(folder.__str__())
                #mt_a.cache()
                if metadata is not None:
                    # mt_a.write(outpath.__str__()) #Hail scripts in here fix loaded MatrixTables
                    # and outputs into new args.out
                   #mt_a.drop('phenotype')
                    #phen, mut = metadata.get(prefix, ["NA", "NA"])
                    #mt_a = mt_a.annotate_globals(metadata=hl.struct(phenotype=phen, mutation=mut))
                    #mt_a.write(outpath.__str__())
                    #mt_a.describe()

                    pass
                hailtables[prefix] = mt_a
                if idx//toolbar_width >= i:
                    sys.stderr.write("[{0}] Done {1}%\n".format("x"*(toolbar_width//10)*(i)+"-"*(toolbar_width//10)*(10-i), idx//toolbar_width*10))
                    i+=1

    # TODO: Slicing
    print("Read {0} HailTables".format(len(hailtables.values())))
    if number == -1:
        number = len(hailtables)
    if phenotype is not None:
        # Union HailTables with a given phenotype, thereby filtering
        sys.stderr.write("Filtering tables based on phenotype \"{0}\"\n".format(phenotype))
        for key, ht in hailtables.items():
            #ecode_phenotype[key] = hl.eval(ht.metadata.phenotype)
            #TODO: Load phenotypes once to increase speed of dict searches
            pass

        #matched_tables = list(filter(lambda k, t: t.startswith(phenotype), ecode_phenotype.items()))
        matched_tables = list(filter(lambda t: hl.eval(t.metadata.phenotype.matches(phenotype)),
                    hailtables.values()))
        if len(matched_tables) > 0:
            sys.stderr.write("Found {0} matching table(s) with given phenotype key\n".format(len(matched_tables)))
            unioned_table = table_join(mts_to_table(matched_tables))
        else:
            sys.stderr.write("NO tables matched to phenotype \"{0}\"\n".format(phenotype))
            phens = list(hl.eval(t.metadata.phenotype) for t in hailtables.values())
            raise KeyError("Phenotype keys available: {0}".format(phens))
    else:
        # Else union all tables
        unioned_table = table_join(mts_to_table(list(hailtables.values())))
        #sys.stderr.write("Writing intermediary unioned table to {0}\n".format(gnomadpath.parent.__str__() + "\gnomad_tb_unioned" + str(unique)))
        #unioned_table.write(gnomadpath.parent.__str__() + "\gnomad_tb_unioned" + str(unique))

    gnomad_tb = gnomad_table(unioned_table)
    if gnomadpath.exists():
        if not overwrite:
            raise FileExistsError(gnomadpath)
        else:
            stderr.write("WARNING: Overwrite is active. Deleting pre-existing filetree {0}\n".format(gnomadpath))
            shutil.rmtree(gnomadpath)
            gnomad_tb.write(gnomadpath.__str__())
    else:
        gnomad_tb.write(gnomadpath.__str__())
        pass
    return gnomad_tb


if __name__ == '__main__':
    try:

        parser = argparse.ArgumentParser(prog="Gnomad frequency table burden analysis pipeline command-line tool using "
                                              "Hail\n{0}".format(hl.cite_hail()))
        subparsers = parser.add_subparsers(title="commands", dest="command")
        findtype = subparsers.add_parser("Findtype", help="Find all specific files of a given filetype.")
        findtype.add_argument("-s", "--source", help="Directory to be searched.", action="store", type=str)
        findtype.add_argument("-d", "--directory", help="Directory to be saved to.", nargs='?', const=".",
                              action="store", type=str)  # TODO: Default value returns None type
        findtype.add_argument("-t", "--type", help="Filetype to be used.", action="store", type=str)
        findtype.add_argument("-r", "--regex", help="Filter strings by including parsed regex.", action="store",
                              type=str)
        readvcfs = subparsers.add_parser("Readvcfs", help="Turn VCF file(s) into a Hail MatrixTable.")
        readvcfs.add_argument("-f", "--file",
                              help="The VCF file(s) [comma seperated], "
                                   ".txt/.list of VCF paths to be parsed or folder containing VCF files.",
                              nargs='+')
        readvcfs.add_argument("-d", "--dest", help="Destination folder to write the Hail MatrixTable files.",
                              nargs='?', const=os.path.abspath("."))
        readvcfs.add_argument("-r", "--overwrite", help="Overwrites any existing output MatrixTables, HailTables.",
                              action="store_true")
        readvcfs.add_argument("-g", "--globals", help="Tab delimited input file containing globals string "
                                                      "for a given unique sample "
                                                      "(e.g. Identifier\\t.Phenotype\\tMutations", action="store",
                              type=str)
        loaddb = subparsers.add_parser("Loaddb", help="Load a folder containing HailTables.")
        loaddb.add_argument("-d", "--directory", help="Folder to load the Hail MatrixTable files from.",
                            nargs='?', const=os.path.abspath("."))
        loaddb.add_argument("-r", "--overwrite", help="Overwrites any existing output MatrixTables, HailTables.",
                            action="store_true")
        loaddb.add_argument("-o", "--out", help="Output destination.", action="store", type=str)
        loaddb.add_argument("-n", "--number", help="Number of tables to be collated.", nargs="?",
                            type=int,
                            default=-1)
        loaddb.add_argument("-g", "--globals", help="Tab delimited input file containing globals string "
                                                    "for a given unique sample.", action="store", type=str)
        loaddb.add_argument("--phenotype", help="Filter a subset of samples with given phenotype. "
                                                "Regex strings accepted e.g. r'NA\d+", action="store",
                            type=str)

        args = parser.parse_args()
        if args.command is not None:

            if str.lower(args.command) == "findtype":
                # print(findtypes(args.directory, args.type))
                files = file_utility.find_filetype(args.source, args.type, verbose=False)

                file_utility.write_filelist(args.directory, "{0}.{1}.txt".format(os.path.basename(
                    os.path.normpath(args.source)), args.type), files, regex=args.regex)
                # Convert the directory into a name for the file, passing found files with
                # Regex in files matching only with a matching regex (e.g. *.vep.vcf wildcard).
                # Unique files only, duplicates written to duplicates_*.txt

            else:
                conf = SparkConf()
                conf.set('spark.sql.files.maxPartitionBytes', '60000000000')
                conf.set('spark.sql.files.openCostInBytes', '60000000000')
                conf.set('spark.submit.deployMode', u'client')
                conf.set('spark.app.name', u'HailTools-TSHC')
                conf.set('spark.executor.memory', "4g")
                conf.set('spark.driver.memory', "56g")
                conf.set("spark.jars", "{0}/backend/hail-all-spark.jar".format(hail_home))
                conf.set("spark.executor.extraClassPath", "./hail-all-spark.jar")
                conf.set("spark.driver.extraClassPath", "{0}/backend/hail-all-spark.jar".format(hail_home))
                conf.set("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
                conf.set("spark.kryo.registrator", "is.hail.kryo.HailKryoRegistrator")
                conf.set("spark.driver.bindAddress", "127.0.0.1")
                conf.set("spark.local.dir", "{0}".format(args.out))
                sc = SparkContext(conf=conf)
                hl.init(backend="spark", sc=sc, min_block_size=128)
                if str.lower(args.command) == "readvcfs":
                    full_paths = [Path(path) for path in args.file]
                    files = set()
                    for path in full_paths:
                        if path.is_file():
                            if path.suffix ==".vcf":  # VCF files are parsed.
                                files.add(path)
                            else:  # might be a list of VCFs
                                with open(path, "r") as filelist:
                                    for line in filelist:
                                        # coerce lines into path
                                        p = Path(line.strip())
                                        if p.suffix == ".vcf":
                                            files.add(p)
                        else:  # Glob folder for *.VCF
                            files |= set(path.glob("/*.vcf"))
                    gnomad_path = Path(args.dest).joinpath(Path("gnomad_tb"))
                    if gnomad_path.exists():
                        if args.overwrite:
                            gnomad_tb = write_gnomad_table(files, args.dest, overwrite=args.overwrite,
                                                           metadata=args.globals)
                        else:
                            FileExistsError("The combined gnomad_tb exists and --overwrite is not active! "
                                            "Rename or move the folder {0}".format(gnomad_path.__str__()))
                    else:
                        gnomad_tb = write_gnomad_table(files, args.dest, overwrite=args.overwrite,
                                                       metadata=args.globals)
                    #gnomad_tb.describe()
                    gnomad_tb.flatten().export(Path(args.dest).parent.joinpath("gnomad.tsv").__str__())
                elif str.lower(args.command) == "loaddb":
                    metadata_dict = None
                    if args.globals is not None:
                        metadata_dict = get_metadata(args.globals)

                    dirpath = Path(args.directory)
                    gnomad_tb = load_hailtables(dirpath, args.number, args.out, metadata_dict, args.overwrite, args.phenotype)
                    gnomad_tb.describe()
                    gnomad_tb.flatten().export(Path(args.out).parent.joinpath("gnomad_tb{0}.tsv".format(unique)).__str__())
        else:
            # No valid command
            parser.print_usage()
            # print("Invalid input, quitting.")
            # assert os.path.exists(mt_path)
            # mt = hl.read_matrix_table(mt_path)

    except KeyboardInterrupt:
        print("Quitting.")
        raise

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
