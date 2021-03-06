#!/usr/bin/python
#
#   this module creates the objects to be tested and kicks off
#   a standard set of tests.  The primary entry point (test) is
#   driven by a set of configuration dictionaries, describing
#   the devices, cluster, and tests to be run
#

# simulations
import SimDisk
import SimFS
import FileStore
import Rados

# test harnesses
import disktest
import fstest
import filestoretest
import radostest

MEG = 1000 * 1000
GIG = 1000 * MEG
TERA = 1000 * GIG


def makedisk(dict):
    """ instantiate the disk described by a configuration dict
            device -- type of device to create (default disk)
            size -- usable space (default 2TB)
            rpm -- rotational speed (default 7200 RPM)
            speed -- max transfer speed (default 150MB/s)
            iops -- max iops
            heads -- number of heads
            streams -- max concurrent streams
            shared -- (for journals, do multiple OSDs share this device)
    """

    # collect universal parameters
    sz = dict['size'] if 'size' in dict else 2 * TERA
    spd = dict['speed'] if 'speed' in dict else 150 * MEG

    if 'device' in dict and dict['device'] == 'ssd':
        iops = dict['iops'] if 'iops' in dict else 20000
        strm = dict['streams'] if 'streams' in dict else 1
        return SimDisk.SSD(sz, spd, iops=iops, streams=strm)
    elif 'device' in dict and dict['device'] == 'dumb':
        rpm = dict['rpm'] if 'rpm' in dict else 7200
        heads = dict['heads'] if 'heads' in dict else 10
        return SimDisk.DumbDisk(rpm, sz, spd, heads=heads)
    else:
        rpm = dict['rpm'] if 'rpm' in dict else 7200
        heads = dict['heads'] if 'heads' in dict else 10
        return SimDisk.Disk(rpm, sz, spd, heads=heads)


def makefs(disk, dict):
    """ instantiate the filesystem described by a configuration dict
        disk -- on which file system is to be created
        dict -- of file system paramters
             -- fs: type of file system
             -- age: 0-1
    """

    age = dict['age'] if 'age' in dict else 0

    if 'fs' in dict and dict['fs'] == 'btrfs':
        return SimFS.btrfs(disk, age)
    elif 'fs' in dict and dict['fs'] == 'ext4':
        return SimFS.ext4(disk, age)
    else:
        return SimFS.xfs(disk, age)


def makerados(filestore, dict):
    """ instantiate the rados cluster described by a configuration dict
        filestore -- on which cluster is based
        dict -- of cluster configuration parameters
             -- nodes: number of nodes in cluster
             -- osd_per_node: number of OSDs per node
             -- front: speed of front-side network
             -- back: speed of back-side network
    """

    return Rados.Rados(filestore,
            front_nic=dict['front'],
            back_nic=dict['back'],
            nodes=dict['nodes'],
            osd_per_node=dict['osd_per_node'])


def test(data, journal, cluster, tests):
    """ run a specific set of tests on a specific cluster simulation
        data -- dictionary describing the data devices
        journal -- dictionary describing the journal devices
        cluster -- dictionary describing the cluster
        tests -- dictionary describing the tests to be run
    """

    # instantiate the data device simulation
    myDDisk = makedisk(data)
    myData = makefs(myDDisk, data)
    data_fstype = myData.__class__.__name__
    data_dev = myDDisk.__class__.__name__
    data_desc = "data FS (%s on %s)" % (data_fstype, data_dev)

    # instantiate the journal device description
    j_share = 1
    if journal != None:
        myJDisk = makedisk(journal)
        myJrnl = makefs(myJDisk, journal)
        jrnl_fstype = myJrnl.__class__.__name__
        jrnl_dev = myJDisk.__class__.__name__
        jrnl_desc = "journal FS (%s on %s)" % (jrnl_fstype, jrnl_dev)
        if 'shared' in journal and journal['shared']:
            j_share = cluster['osd_per_node']
    else:
        myJrnl = None
        jrnl_desc = "journal on data disk"

    # instantiate the filestore
    journal_share = 1 if not j_share else cluster['osd_per_node']
    myFstore = FileStore.FileStore(myData, myJrnl, journal_share=j_share)

    # instantiate the RADOS simulation
    myRados = makerados(myFstore, cluster)

    #
    # run the specified tests for the specified ranges
    #

    if 'DiskParms' in tests and tests['DiskParms']:
        if myJrnl is not None:
            print("Journal Device Characteristics")
            disktest.disktest(myJrnl.disk)
            print("")
        print("Data Device Characteristics")
        disktest.disktest(myData.disk)
        print("")

    sz = tests['FioRsize']
    if myJrnl is not None and 'FioJournal' in tests and tests['FioJournal']:
        for d in tests['FioRdepths']:
            print("Raw journal device (%s), depth=%d" % (jrnl_dev, d))
            disktest.tptest(myJrnl.disk, filesize=sz, depth=d)
            print("")

    for d in tests['FioRdepths']:
        print("Raw data device (%s), depth=%d" % (data_dev, d))
        disktest.tptest(myData.disk, filesize=sz, depth=d)
        print("")

    sz = tests['FioFsize']
    if myJrnl is not None and 'FioJournal' in tests and tests['FioJournal']:
        for d in tests['FioFdepths']:
            print("FIO (direct) to %s, depth=%d" % (jrnl_desc, d))
            fstest.fstest(myJrnl, filesize=sz, depth=d, direct=True)
            print("")

    for d in tests['FioFdepths']:
        print("FIO (direct) to %s, depth=%d" % (data_desc, d))
        fstest.fstest(myFstore.data_fs, filesize=sz, depth=d, direct=True)
        print("")

    msg = "smalliobench-fs, %s, %s%s, depth=%d"
    sz = tests['SioFsize']
    no = tests['SioFnobj']
    for d in tests['SioFdepths']:
        print(msg % (data_desc, jrnl_desc,
                    "" if j_share == 1 else "/%d" % (j_share), d))
        print("\tnobj=%d, objsize=%d" % (no, sz))
        filestoretest.fstoretest(myFstore, nobj=no, obj_size=sz, depth=d)
        print("")

    msg = "smalliobench-rados (%dx%d), %d copy, clients*instances*depth=(%d*%d*%d)"
    sz = tests['SioRsize']
    no = tests['SioRnobj']
    for x in tests['SioRcopies']:
        for c in tests['SioRclients']:
            for i in tests['SioRinstances']:
                for d in tests['SioRdepths']:
                    print(msg %
                        (myRados.num_nodes, myRados.osd_per_node,
                        x, c, i, d))
                    print("\t%s, %s%s, nobj=%d, objsize=%d" %
                            (data_desc, jrnl_desc,
                            "" if j_share == 1 else "/%d" % (j_share),
                            no, sz))
                    radostest.radostest(myRados, obj_size=sz, nobj=no,
                                        clients=c, depth=i * d, copies=x)
                    print("")

    # check for warnings
    if myFstore.warnings != "" or myRados.warnings != "":
        print("WARNINGS: %s%s" % (myFstore.warnings, myRados.warnings))


#
# standard test parameters and usage example
#
if __name__ == '__main__':

    data = {        # data storage devices
        'device': "disk",
        'fs': "xfs"
    }

    journal = {     # journal devices
        'device': "ssd",
        'size': 1 * GIG,
        'speed': 400 * MEG,
        'iops': 30000,
        'streams': 8,
        'fs': "xfs",
        'shared': True
    }

    cluster = {     # cluster configuration
        'front': 1 * GIG,
        'back': 10 * GIG,
        'nodes': 4,
        'osd_per_node': 4
    }

    tests = {       # what tests to run with what parameters
        # raw disk parameters and simulations
        'DiskParms': True,
        'FioRdepths': [1, 32],
        'FioRsize': 16 * GIG,

        # FIO performance tests
        'FioJournal': True,
        'FioFdepths': [1, 32],
        'FioFsize': 16 * GIG,

        # filestore performance tests
        'SioFdepths': [16],
        'SioFsize': 1 * GIG,
        'SioFnobj': 2500,

        # RADOS performance tests
        'SioRdepths': [16],
        'SioRsize': 1 * GIG,
        'SioRnobj': 2500 * 4 * 4,   # multiply by number of OSDs
        'SioRcopies': [2],
        'SioRclients': [3],
        'SioRinstances': [4]
    }

    test(data, journal, cluster, tests)
