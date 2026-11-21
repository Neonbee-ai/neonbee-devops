"""Load a Lambda's index.py under a unique module name (all three are called index)."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    path = os.path.join(HERE, "..", "lambdas", name, "index.py")
    spec = importlib.util.spec_from_file_location("lambda_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
