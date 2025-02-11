# Last Updated: 2022-04-05

import sys
import os
from importlib import reload

sys.path.insert(0, '..'+os.sep)
from eureka.lib.readECF import MetaClass
from eureka.lib.util import pathdirectory
import eureka.lib.plots
from eureka.S3_data_reduction import s3_reduce as s3


def test_WFC3(capsys):
    # Set up some parameters to make plots look nicer.
    # You can set usetex=True if you have LaTeX installed
    eureka.lib.plots.set_rc(style='eureka', usetex=False, filetype='.pdf')

    with capsys.disabled():
        # is able to display any message without failing a test
        # useful to leave messages for future users who run the tests
        print("\n\nIMPORTANT: Make sure that any changes to the ecf files "
              "are\nincluded in demo ecf files and documentation "
              "(docs/source/ecf.rst).")
        print("\nWFC3 S3 test: ", end='', flush=True)

    # explicitly define meta variables to be able to run
    # pathdirectory fn locally
    meta = MetaClass()
    meta.eventlabel = 'WFC3'
    meta.topdir = f'..{os.sep}tests'
    ecf_path = f'.{os.sep}WFC3_ecfs{os.sep}'

    reload(s3)
    s3.reduce(meta.eventlabel, ecf_path=ecf_path)

    # run assertions for S3
    meta.outputdir_raw = f'data{os.sep}WFC3{os.sep}Stage3{os.sep}'
    name = pathdirectory(meta, 'S3', 1, ap=8, bg=40)
    assert os.path.exists(name)
    assert os.path.exists(name+os.sep+'figs')

    # remove temporary files
    os.system(f"rm -r data{os.sep}WFC3{os.sep}Stage3")
