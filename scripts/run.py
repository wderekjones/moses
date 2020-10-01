import os
import argparse
import sys
import importlib.util
import pandas as pd

from moses.models_storage import ModelsStorage

def load_module(name, path):
    dirname = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(dirname, path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module



MODELS = ModelsStorage()


split_dataset = load_module("split_dataset", "split_dataset.py")
eval_script = load_module("eval", "metrics/eval.py")
trainer_script = load_module("train", "train.py")
sampler_script = load_module("sample", "sample.py")



def get_model_path(config, model):
    return os.path.join(
        config.checkpoint_dir, model + config.experiment_suff + "_model.pt"
    )


def get_config_path(config, model):
    return os.path.join(
        config.checkpoint_dir, model + config.experiment_suff + "_config.pt"
    )


def get_vocab_path(config, model):
    return os.path.join(
        config.checkpoint_dir, model + config.experiment_suff + "_vocab.pt"
    )


def get_generation_path(config, model):
    if(config.lbann_weights_dir):
      return os.path.join(config.lbann_weights_dir + 'e' + str(config.lbann_epoch_counts) + model + config.experiment_suff + '_generated.csv')
    else :
      return os.path.join(config.data_dir, model + config.experiment_suff + '_generated.csv')

def get_reconstruction_path(config, model):
    if(config.lbann_weights_dir):
      return os.path.join(config.lbann_weights_dir + 'e' + str(config.lbann_epoch_counts) + model + config.experiment_suff + '_predicted.csv')
    else :
      return os.path.join(config.data_dir, model + config.experiment_suff + '_predicted.csv')


def get_device(config):
    return f"cuda:{config.gpu}" if config.gpu >= 0 else "cpu"


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=str,
        default="all",
        choices=["all"] + MODELS.get_model_names(),
        help="Which model to run",
    )
    parser.add_argument(
        "--data_dir", type=str, default="../data", help="Directory for datasets"
    )
    parser.add_argument(
        "--checkpoint_dir",
        type=str,
        default="./checkpoints",
        help="Directory for checkpoints",
    )
    parser.add_argument(
        "--n_samples", type=int, default=1000, help="Number of samples to sample"
    )
    parser.add_argument("--n_jobs", type=int, default=1, help="Number of threads")
    parser.add_argument("--gpu", type=int, default=-1, help="GPU index (-1 for cpu)")
    parser.add_argument(
        "--metrics",
        type=str,
        default="metrics.csv",
        help="Path to output file with metrics",
    )
    parser.add_argument(
        "--train_size", type=int, default=None, help="Size of training dataset"
    )
    parser.add_argument(
        "--test_size", type=int, default=None, help="Size of testing dataset"
    )
    parser.add_argument(
        "--experiment_suff",
        type=str,
        default="",
        help="Experiment suffix to break ambiguity",
    )
    parser.add_argument(
        "--vocab-path", type=str, default="", help="path to experiment vocabulary"
    )
    parser.add_argument(
        "--model-path", type=str, default="", help="path to experiment model"
    )
    parser.add_argument(
        "--config-path", type=str, default="", help="path to experiment config"
    )
    parser.add_argument(
        "--lbann_weights_dir",
        type=str,
        default='',
        help="Directory for LBANN weights for inference",
    )
    parser.add_argument(
        "--lbann_epoch_counts",
        type=int,
        default=-1,
        required=False,
        help="LBANN epoch count at which to load trained model,not required if only one trained model is available",
    )
    parser.add_argument("--save_reconstruction", action='store_true')
    return parser


# UNDER CONSTRUCTION FOR COMPATIBILITY WITH LOADING AND TRAINING LBANN MODELS
def train_model(config, model, train_path):
    model_path = get_model_path(config, model)
    config_path = get_config_path(config, model)
    vocab_path = get_vocab_path(config, model)
    log_path = get_log_path(config, model)

    #vocab_path = config.vocab_path    

    if os.path.exists(model_path) and \
            os.path.exists(config_path) and \
            os.path.exists(vocab_path):
        return

    trainer_parser = trainer_script.get_parser()
    args = [
        '--device', config.device,
        '--model_save', model_path,
        '--config_save', config_path,
        '--vocab_save', vocab_path,
        '--log_file', log_path,
        '--n_jobs', str(config.n_jobs)
    ]
    if train_path is not None:
        args.extend(['--train_load', train_path])
    if test_path is not None:
        args.extend(['--val_load', test_path])

    trainer_config = trainer_parser.parse_known_args(
         [model] + sys.argv[1:] + args
    )[0]
    trainer_script.main(model, trainer_config)


def sample_from_model(config, model,test_path):
    model_path = config.model_path if config.model_path != '' else get_model_path(config, model)
    config_path = config.config_path if config.config_path != '' else get_config_path(config, model)
    vocab_path = config.vocab_path if config.vocab_path != '' else get_vocab_path(config, model)

    assert os.path.exists(
        model_path
    ), "Can't find model path for sampling: '{}'".format(model_path)
    assert os.path.exists(
        config_path
    ), "Can't find config path for sampling: '{}'".format(config_path)
    assert os.path.exists(
        vocab_path
    ), "Can't find vocab path for sampling: '{}'".format(vocab_path)

    if config.lbann_weights_dir:
        assert os.path.exists(config.lbann_weights_dir), (
            "LBANN inference mode is specified but directory "
            " to load weights does not exist: '{}'".format(config.lbann_weights_dir)
        )

    sampler_parser = sampler_script.get_parser()
    sampler_config = sampler_parser.parse_known_args(
        [model,]
        + sys.argv[1:]
        + [
            "--device",
            get_device(config),
            "--model_load",
            model_path,
            "--config_load",
            config_path,
            "--vocab_load",
            vocab_path,
            "--lbann_weights_dir",
            str(config.lbann_weights_dir),
            "--lbann_epoch_counts",
            str(config.lbann_epoch_counts),
            "--gen_save",
            get_generation_path(config, model),
            "--pred_save",
            get_reconstruction_path(config, model),
            "--n_samples",
            str(config.n_samples),
            '--test_path', 
            test_path,
        ]
    )[0]
    sampler_script.main(model, sampler_config)


def eval_metrics(
    config, model, test_path, test_scaffolds_path, ptest_path, ptest_scaffolds_path
):
    eval_parser = eval_script.get_parser()
    eval_config = eval_parser.parse_args(
        [
            "--test_path",
            test_path,
            "--test_scaffolds_path",
            test_scaffolds_path,
            "--ptest_path",
            ptest_path,
            "--ptest_scaffolds_path",
            ptest_scaffolds_path,
            "--gen_path",
            get_generation_path(config, model),
            "--n_jobs",
            str(config.n_jobs),
            "--gpu",
            str(config.gpu),
        ]
    )
    metrics = eval_script.main(eval_config, print_metrics=False)

    return metrics


def main(config):
    if not os.path.exists(config.checkpoint_dir):
        os.mkdir(config.checkpoint_dir)

    train_path = os.path.join(config.data_dir, "train10k.csv")
    test_path = os.path.join(config.data_dir, "test.csv")
    test_scaffolds_path = os.path.join(config.data_dir, "test_scaffolds.csv")
    ptest_path = os.path.join(config.data_dir, "test_stats.npz")
    ptest_scaffolds_path = os.path.join(config.data_dir, "test_scaffolds_stats.npz")

    if (
        not os.path.exists(train_path)
        or not os.path.exists(test_path)
        or not os.path.exists(test_scaffolds_path)
    ):
        splitting_config = split_dataset.get_parser()
        conf = ["--dir", config.data_dir]
        if config.train_size is not None:
            conf.extend(["--train_size", str(config.train_size)])
        if config.test_size is not None:
            conf.extend(["--test_size", str(config.test_size)])
        splitting_config = splitting_config.parse_args(conf)
        split_dataset.main(splitting_config)

    models = MODELS.get_model_names() if config.model == "all" else [config.model]
    for model in models:
        print(f"lbann weights dir: {config.lbann_weights_dir}")
        if not os.path.exists(config.lbann_weights_dir):  # LBANN is inference only
            train_model(config, model, train_path)
        sample_from_model(config, model,test_path)
    metrics = []
    for model in models:
        model_metrics = eval_metrics(
            config,
            model,
            test_path,
            test_scaffolds_path,
            ptest_path,
            ptest_scaffolds_path,
        )
        model_metrics.update({"model": model})
        metrics.append(model_metrics)
    
    table = pd.DataFrame(metrics)
    print("Saving computed metrics to ", config.metrics)
    table.to_csv(config.metrics, index=False)
if __name__ == "__main__":
    parser = get_parser()
    config = parser.parse_known_args()[0]
    main(config)
