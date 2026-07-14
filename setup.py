from setuptools import find_packages, setup

setup(
    name="fchelper",
    version="0.4.0",
    packages=find_packages(),
    description="Helpers for the Funder's Club applications",
    url="https://github.com/fundersclub/fundersclub-helpers",
    install_requires=[
        "boto3",
        "requests",
    ],
)
