#!/usr/bin/env python3

"""
Script containing CIME python regression test suite. This suite should be run
to confirm overall CIME correctness.
"""

import glob, os, re, shutil, signal, sys, tempfile, \
    threading, time, logging, unittest, getpass, \
    filecmp, time, atexit, functools

CIMEROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, CIMEROOT)

from xml.etree.ElementTree import ParseError

import subprocess, argparse
subprocess.call('/bin/rm -f $(find . -name "*.pyc")', shell=True, cwd=CIMEROOT)
import six
from six import assertRaisesRegex
import stat as osstat

import collections

from CIME.utils import run_cmd, run_cmd_no_fail, get_lids, get_current_commit, \
    safe_copy, CIMEError, get_cime_root, get_src_root, Timeout, \
    import_from_file, get_model
import get_tests
import CIME.test_scheduler, CIME.wait_for_tests
from  CIME.test_scheduler import TestScheduler
from  CIME.XML.compilers import Compilers
from  CIME.XML.env_run import EnvRun
from  CIME.XML.machines import Machines
from  CIME.XML.files import Files
from  CIME.case import Case
from  CIME.code_checker import check_code, get_all_checkable_files
from  CIME.test_status import *
from  CIME.provenance import get_test_success, save_test_success

os.environ["CIME_GLOBAL_WALLTIME"] = "0:05:00"


def write_provenance_info(machine, test_compiler, test_mpilib, test_root):
    curr_commit = get_current_commit(repo=CIMEROOT)
    logging.info("\nTesting commit %s" % curr_commit)
    cime_model = get_model()
    logging.info("Using cime_model = %s" % cime_model)
    logging.info("Testing machine = %s" % machine)
    if test_compiler is not None:
        logging.info("Testing compiler = %s"% test_compiler)
    if test_mpilib is not None:
        logging.info("Testing mpilib = %s"% test_mpilib)
    logging.info("Test root: %s" % test_root)
    logging.info("Test driver: %s" % CIME.utils.get_cime_default_driver())
    logging.info("Python version {}\n".format(sys.version))


def cleanup(test_root):
    if os.path.exists(test_root):
        testreporter = os.path.join(test_root,"testreporter")
        files = os.listdir(test_root)
        if len(files)==1 and os.path.isfile(testreporter):
            os.unlink(testreporter)
        if not os.listdir(test_root):
            print("All pass, removing directory:", test_root)
            os.rmdir(test_root)

def _main_func(description):
    config = CIME.utils.get_cime_config()

    help_str = \
"""
{0} [TEST] [TEST]
OR
{0} --help

\033[1mEXAMPLES:\033[0m
    \033[1;32m# Run the full suite \033[0m
    > {0}

    \033[1;32m# Run all code checker tests \033[0m
    > {0} B_CheckCode

    \033[1;32m# Run test test_wait_for_test_all_pass from class M_TestWaitForTests \033[0m
    > {0} M_TestWaitForTests.test_wait_for_test_all_pass
""".format(os.path.basename(sys.argv[0]))

    parser = argparse.ArgumentParser(usage=help_str,
                                     description=description,
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("--fast", action="store_true",
                        help="Skip full system tests, which saves a lot of time")

    parser.add_argument("--no-batch", action="store_true",
                        help="Do not submit jobs to batch system, run locally."
                        " If false, will default to machine setting.")

    parser.add_argument("--no-fortran-run", action="store_true",
                        help="Do not run any fortran jobs. Implies --fast"
                        " Used for github actions")

    parser.add_argument("--no-cmake", action="store_true",
                        help="Do not run cmake tests")

    parser.add_argument("--no-teardown", action="store_true",
                        help="Do not delete directories left behind by testing")

    parser.add_argument("--machine",
                        help="Select a specific machine setting for cime", default=None)

    parser.add_argument("--compiler",
                        help="Select a specific compiler setting for cime", default=None)

    parser.add_argument( "--mpilib",
                        help="Select a specific compiler setting for cime", default=None)

    parser.add_argument( "--test-root",
                        help="Select a specific test root for all cases created by the testing", default=None)

    parser.add_argument("--timeout", type=int,
                        help="Select a specific timeout for all tests", default=None)

    ns, args = parser.parse_known_args()

    # Now set the sys.argv to the unittest_args (leaving sys.argv[0] alone)
    sys.argv[1:] = args

    if ns.timeout:
        os.environ["GLOBAL_TIMEOUT"] = str(ns.timeout)
    os.environ["NO_FORTRAN_RUN"] = str(ns.no_fortran_run or False)
    os.environ["FAST_ONLY"] = str((ns.fast or ns.no_fortran_run) or False)
    os.environ["NO_BATCH"] = str(ns.no_batch or False)
    os.environ["NO_CMAKE"] = str(ns.no_cmake or False)
    os.environ["NO_TEARDOWN"] = str(ns.no_teardown or False)

    os.chdir(CIMEROOT)

    if ns.machine is not None:
        MACHINE = Machines(machine=ns.machine)
    elif config.has_option("create_test", "MACHINE"):
        MACHINE = Machines(config.get("create_test", "MACHINE"))
    elif config.has_option("main", "MACHINE"):
        MACHINE = Machines(config.get("main", "MACHINE"))
    else:
        MACHINE = Machines()

    os.environ["CIME_MACHINE"] = MACHINE.get_machine_name()

    if ns.compiler is not None:
        TEST_COMPILER = ns.compiler
    elif config.has_option("create_test", "COMPILER"):
        TEST_COMPILER = config.get("create_test", "COMPILER")
    elif config.has_option("main", "COMPILER"):
        TEST_COMPILER = config.get("main", "COMPILER")
    else:
        TEST_COMPILER = MACHINE.get_default_compiler()

    os.environ["TEST_COMPILER"] = TEST_COMPILER

    if ns.mpilib is not None:
        TEST_MPILIB = ns.mpilib
    elif config.has_option("create_test", "MPILIB"):
        TEST_MPILIB = config.get("create_test", "MPILIB")
    elif config.has_option("main", "MPILIB"):
        TEST_MPILIB = config.get("main", "MPILIB")
    else:
        TEST_MPILIB = MACHINE.get_default_MPIlib()

    os.environ["TEST_MPILIB"] = TEST_MPILIB

    if ns.test_root is not None:
        TEST_ROOT = ns.test_root
    elif config.has_option("create_test", "TEST_ROOT"):
        TEST_ROOT = config.get("create_test", "TEST_ROOT")
    else:
        TEST_ROOT = os.path.join(MACHINE.get_value("CIME_OUTPUT_ROOT"),
                                 "scripts_regression_test.%s"% CIME.utils.get_timestamp())

    os.environ["TEST_ROOT"] = TEST_ROOT

    args = lambda: None # just something to set attrs on
    for log_param in ["debug", "silent", "verbose"]:
        flag = "--%s" % log_param
        if flag in sys.argv:
            sys.argv.remove(flag)
            setattr(args, log_param, True)
        else:
            setattr(args, log_param, False)

    args = CIME.utils.parse_args_and_handle_standard_logging_options(args, None)

    write_provenance_info(MACHINE, TEST_COMPILER, TEST_MPILIB, TEST_ROOT)

    atexit.register(functools.partial(cleanup, TEST_ROOT))

    test_suite = unittest.defaultTestLoader.discover(CIMEROOT)
    test_runner = unittest.TextTestRunner(verbosity=2)

    test_runner.run(test_suite)

    TEST_RESULT = test_runner._makeResult()
    # Implements same behavior as unittesst.main
    # https://github.com/python/cpython/blob/b6d68aa08baebb753534a26d537ac3c0d2c21c79/Lib/unittest/main.py#L272-L273
    sys.exit(not TEST_RESULT.wasSuccessful())

if (__name__ == "__main__"):
    _main_func(__doc__)
