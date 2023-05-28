from distutils.core import setup
from distutils import util

setup(
    name="TypeT5",
    version="0.1",
    packages=["typet5", "typet5.experiments"],
    package_dir={
        "typet5": util.convert_path("src/typet5"),
        "typet5.experiments": util.convert_path("src/typet5/experiments")
    },
    license="BSD 3-Clause",
)
