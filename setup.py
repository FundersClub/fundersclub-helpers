from setuptools import find_packages, setup

setup(
    name="fchelper",
    version="0.5.0",
    packages=find_packages(),
    description="Helpers for the Funder's Club applications",
    url="https://github.com/fundersclub/fundersclub-helpers",
    # tomllib (pointers file) is 3.11+.
    python_requires=">=3.11",
    install_requires=[
        "boto3",
        "requests",
    ],
)
