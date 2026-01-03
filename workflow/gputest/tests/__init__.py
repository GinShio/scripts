import unittest


def load_tests(loader, tests, pattern):
    return loader.discover('.', pattern=pattern)
