#!/usr/bin/env python

import argparse
import glob
import os

import pandas as pd
import pkg_resources
import ruamel.yaml as yaml
import sys

import logging

from shutil import copyfile
from pyace.generalfit import GeneralACEFit
from pyace.preparedata import get_reference_dataset, sizeof_fmt
from pyace import __version__, get_ace_evaluator_version
from pyace.atomicenvironment import calculate_minimal_nn_atomic_env, calculate_minimal_nn_tp_atoms
from pyace.validate import plot_analyse_error_distributions

files_to_remove = ["fitting_data_info.csv", "log.txt", "nohup.out",
                   "target_potential.yaml", "current_extended_potential.yaml", "output_potential.yaml",
                   "ladder_metrics.txt", "cycle_metrics.txt", "metrics.txt",
                   "test_ladder_metrics.txt", "test_cycle_metrics.txt", "test_metrics.txt",
                   "train_pred.pckl.gzip", "test_pred.pckl.gzip"
                   ]

DEFAULT_SEED = 42

log = logging.getLogger()


def main(args):
    parser = argparse.ArgumentParser(prog="pacemaker", description="Fitting utility for atomic cluster expansion "
                                                                   "potentials.\n" +
                                                                   "version: {}".format(__version__))
    parser.add_argument("input", help="input YAML file, default: input.yaml", nargs='?', type=str, default="input.yaml")

    parser.add_argument("-c", "--clean", help="Remove all generated data",
                        dest="clean", default=False,
                        action="store_true")

    parser.add_argument("-o", "--output", help="output B-basis YAML file name, default: output_potential.yaml",
                        default="output_potential.yaml",
                        type=str)

    parser.add_argument("-p", "--potential",
                        help="input potential YAML file name, will override input file 'potential' section",
                        type=str,
                        default=argparse.SUPPRESS)

    parser.add_argument("-ip", "--initial-potential",
                        help="initial potential YAML file name, will override input file 'potential::initial_potential' section",
                        type=str,
                        default=argparse.SUPPRESS)

    parser.add_argument("-b", "--backend",
                        help="backend evaluator, will override section 'backend::evaluator' from input file",
                        type=str,
                        default=argparse.SUPPRESS)

    parser.add_argument("-d", "--data",
                        help="data file, will override section 'YAML:fit:filename' from input file",
                        type=str,
                        default=argparse.SUPPRESS)

    parser.add_argument("--query-data", help="query the training data from database, prepare and save them",
                        dest="query_data", default=False,
                        action="store_true")

    parser.add_argument("--prepare-data", help="prepare and save training data only", dest="prepare_data",
                        default=False,
                        action="store_true")

    parser.add_argument("--rebuild", help="force to rebuild necessary neighbour lists",
                        dest="force_rebuild",
                        default=False,
                        action="store_true"
                        )

    parser.add_argument("-l", "--log", help="log filename, default: log.txt", type=str, default="log.txt")

    parser.add_argument("-dr", "--dry-run",
                        help="Dry run: performs all preprocessing and analysis, but do not do the fitting",
                        dest="dry_run", action="store_true",
                        default=False)

    parser.add_argument("-t", "--template",
                        help="Create a template 'input.yaml' file",
                        dest="template", action="store_true",
                        default=False)

    parser.add_argument("-v", "--version",
                        help="Show version info",
                        dest="version", action="store_true",
                        default=False)

    parser.add_argument("--no-fit", help="Do not fit the potential", dest="no_fit",
                        action="store_true", default=False)

    parser.add_argument("--no-predict", help="Do not compute and save the predictions", dest="no_predict",
                        action="store_true", default=False)

    parser.add_argument("--verbose-tf", help="Make tensorflow more verbose (off by defeault)",
                        dest="verbose_tf", action="store_true", default=False
                        )
    args_parse = parser.parse_args(args)

    input_yaml_filename = args_parse.input

    if not args_parse.verbose_tf:
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

    if args_parse.version:
        print("pacemaker/pyace version: {}".format(__version__))
        print("ace_evaluator   version: {}".format(get_ace_evaluator_version()))
        sys.exit(0)

    if args_parse.clean:
        print("Cleaning working directory...")

        interim_potentails = glob.glob("interim_potential*.yaml")
        ensemble_potentails = glob.glob("ensemble_potential*.yaml")
        for filename in sorted(files_to_remove + interim_potentails + ensemble_potentails):
            if os.path.isfile(filename):
                os.remove(filename)
                print(" - " + filename)
        print("done")
        sys.exit(0)

    if args_parse.template:
        print("Creating template 'input.yaml'...", end="")
        template_input_yaml_filename = pkg_resources.resource_filename('pyace.data', 'input_template.yaml')
        copyfile(template_input_yaml_filename, "input.yaml")
        print("done")
        sys.exit(0)

    if args_parse.dry_run:
        log.info("====== DRY RUN ======")

    output_file_name = args_parse.output

    if "log" in args_parse:
        log_file_name = args_parse.log
        log.info("Redirecting log into file {}".format(log_file_name))
        fileh = logging.FileHandler(log_file_name, 'a')
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fileh.setFormatter(formatter)
        # log = logging.getLogger()
        log.addHandler(fileh)

    log.info("Start pacemaker")
    log.info("pacemaker/pyace version: {}".format(__version__))
    log.info("ace_evaluator   version: {}".format(get_ace_evaluator_version()))
    log.info("Loading {}... ".format(input_yaml_filename))
    with open(input_yaml_filename) as f:
        args_yaml = yaml.safe_load(f)

    assert isinstance(args_yaml, dict)
    if "cutoff" in args_yaml:
        cutoff = args_yaml["cutoff"]
    else:
        log.warning("No 'cutoff' provided in YAML file, please specify it")
        raise ValueError("No 'cutoff' provided in YAML file, please specify it")

    if "seed" in args_yaml:
        seed = args_yaml["seed"]
    else:
        seed = DEFAULT_SEED
        log.warning("No 'seed' provided in YAML file, default value seed = {} will be used.".format(seed))

    # data section
    if "data" in args_parse:
        data_config = {"filename": args_parse.data}
        log.info("Overwrite 'data' with " + str(data_config))
    elif "data" in args_yaml:
        data_config = args_yaml["data"]
        if isinstance(data_config, str):
            data_config = {"filename": data_config}
        if "seed" not in data_config:
            data_config["seed"] = seed
    else:
        raise ValueError("'data' section is not provided neither in input file nor in arguments")
    # check environment variable PACEMAKERDATAPATH for absolute path to data
    env_data_path = os.environ.get("PACEMAKERDATAPATH")
    if env_data_path is not None:
        if not data_config.get("datapath"):
            data_config["datapath"] = env_data_path
            log.info("Data path set to $PACEMAKERDATAPATH = {}".format(env_data_path))

    # backend section
    backend_config = {}
    if "backend" in args_yaml:
        backend_config = args_yaml["backend"]
    elif not args_parse.query_data:
        backend_config["evaluator"] = "pyace"
        backend_config["parallel_mode"] = "process"
        log.warning("'backend' is not specified, default settings will be used: {}".format(backend_config))
        # raise ValueError("'backend' section is not given")

    if "backend" in args_parse:
        backend_config["evaluator"]=args_parse.backend
        log.info("Backend settings is overwritten from arguments: ", backend_config)


    if 'evaluator' in backend_config:
        evaluator_name = backend_config['evaluator']
    else:
        backend_config['evaluator'] = 'pyace'
        evaluator_name = backend_config['evaluator']
        log.info("Couldn't find evaluator ('pyace' or 'tensorpot').")
        log.info("Default evaluator `{}` would be used, ".format(evaluator_name) +
                 " otherwise please specify in YAML:backend:evaluator or as -b <evaluator>")

    if args_parse.force_rebuild:
        if isinstance(data_config, str):
            raise ValueError("Requires YAML input file with 'data' section")
        data_config["force_rebuild"] = args_parse.force_rebuild
        log.info("Set force_rebuild = {}".format(args_parse.force_rebuild))

    if args_parse.query_data:
        if isinstance(data_config, str):
            raise ValueError("Requires YAML input file with 'data' section")
        log.debug("data_config={}".format(str(data_config)))
        log.debug("evaluator_name={}".format(evaluator_name))
        log.debug("cutoff={}".format(cutoff))
        get_reference_dataset(evaluator_name=evaluator_name, data_config=data_config, cutoff=cutoff, force_query=True,
                              cache_ref_df=True)
        log.info("Done, now stopping")
        sys.exit(0)

    if args_parse.prepare_data:
        if isinstance(data_config, str):
            raise ValueError("Requires YAML input file with 'data' section")
        log.debug("data_config={}".format(str(data_config)))
        log.debug("evaluator_name={}".format(evaluator_name))
        log.debug("cutoff={}".format(cutoff))
        get_reference_dataset(evaluator_name=evaluator_name, data_config=data_config, cutoff=cutoff, force_query=False,
                              cache_ref_df=True)
        log.info("Done, now stopping")
        sys.exit(0)

    # potential section
    if "potential" in args_parse:
        potential_config = args_parse.potential
        log.info("Potential settings is overwritten from arguments: " + str(potential_config))
    elif "potential" in args_yaml:
        potential_config = args_yaml["potential"]
        if isinstance(potential_config, dict):
            if "metadata" in args_yaml:
                potential_config["metadata"] = args_yaml["metadata"]
    elif not args_parse.query_data:
        raise ValueError("'potential' section is not given")

    if "initial_potential" in args_parse:
        if isinstance(potential_config, dict):
            potential_config["initial_potential"] = args_parse.initial_potential
        else:
            raise ValueError("Couldn't combine `initial_potential` setting with non-dictionary `potential` setting")

    # fit section
    fit_config = {}
    if "fit" in args_yaml:
        fit_config = args_yaml["fit"]
    callbacks = []
    if "callbacks" in fit_config:
        callbacks = fit_config["callbacks"]

    general_fit = GeneralACEFit(potential_config=potential_config, fit_config=fit_config, data_config=data_config,
                                backend_config=backend_config, seed=seed, callbacks=callbacks)
    if args_parse.dry_run:
        log.info("Dry run is finished")
        sys.exit(0)

    if not args_parse.no_fit:
        general_fit.fit()
        general_fit.save_optimized_potential(output_file_name)

    if not args_parse.no_predict:
        log.info("Making predictions")

        # if fit was not done - just take target_bbasisconfig
        target_bbasisconfig = None
        if args_parse.no_fit:
            target_bbasisconfig = general_fit.target_bbasisconfig

        if general_fit.fitting_data is not None:
            log.info("For train data")
            pred_data = predict_and_save(general_fit, target_bbasisconfig, general_fit.fitting_data,
                                         fname="train_pred.pckl.gzip")
            log.info("Ploting validation graphs")
            plot_analyse_error_distributions(pred_data, fig_prefix="train_",fig_path="report",
                                             imagetype=backend_config.get("imagetype","png"))

        if general_fit.test_data is not None:
            log.info("For test data")
            pred_data = predict_and_save(general_fit, target_bbasisconfig, general_fit.test_data,
                                         fname="test_pred.pckl.gzip")
            log.info("Ploting validation graphs")
            plot_analyse_error_distributions(pred_data, fig_prefix="test_", fig_path="report",
                                             imagetype=backend_config.get("imagetype","png"))


def predict_and_save(general_fit, target_bbasisconfig, structures_dataframe, fname):
    pred_data = general_fit.predict(structures_dataframe=structures_dataframe,
                                    bbasisconfig=target_bbasisconfig)
    if not isinstance(pred_data, pd.DataFrame):
        from pyace.const import ENERGY_PRED_COL, FORCES_PRED_COL
        pred_data = pd.DataFrame({ENERGY_PRED_COL: pred_data[ENERGY_PRED_COL],
                                  FORCES_PRED_COL: pred_data[FORCES_PRED_COL]
                                  }, index=structures_dataframe.index)
    columns_to_drop = ["ase_atoms", "atomic_env", "tp_atoms"]

    if general_fit.evaluator_name == "pyace" and "atomic_env" in structures_dataframe.columns:
        log.info("Computing nearest neighbours distances from 'atomic_env'")
        structures_dataframe["nn_min"] = structures_dataframe["atomic_env"].map(calculate_minimal_nn_atomic_env)
    elif general_fit.evaluator_name == "tensorpot" and "tp_atoms" in structures_dataframe.columns:
        log.info("Computing nearest neighbours distances from 'tp_atoms'")
        structures_dataframe["nn_min"] = structures_dataframe["tp_atoms"].map(calculate_minimal_nn_tp_atoms)
    else:
        log.error("No neighbour lists found, could not compute nearest neighbours distances")

    columns_to_drop = [column for column in columns_to_drop if column in structures_dataframe]
    pred_data = pd.merge(structures_dataframe.drop(columns=columns_to_drop), pred_data,
                         left_index=True, right_index=True)
    pred_data.to_pickle(fname, compression="gzip", protocol=4)
    log.info("Predictions are saved into {} ({})".format(fname, sizeof_fmt(fname)))
    return pred_data


if __name__ == "__main__":
    main(sys.argv[1:])
