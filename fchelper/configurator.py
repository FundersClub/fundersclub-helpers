import os

import boto3


configurations = {}


def load(environment: str, prefix: str):
    """
    Fetch secrets from AWS parameter store
    """

    if environment in ("developer", "test"):
        # Developer just uses defaults stored in the repo
        return {}

    # Specify the parameter prefix
    parameter_prefix = f"/{prefix}{environment}/"
    client = boto3.client("ssm", region_name="us-east-1")

    try:
        # Parameters come in pages, so we need to iteratively get the next page with NextToken
        # until all parameters are loaded
        next_token = "__placeholder__"
        while next_token:
            if next_token == "__placeholder__":
                # Get the list of parameters
                resp = client.get_parameters_by_path(Path=parameter_prefix)
            else:
                resp = client.get_parameters_by_path(Path=parameter_prefix, NextToken=next_token)

            # Parse the parameters
            for param in resp.get("Parameters"):
                name = param.get("Name").lstrip(parameter_prefix)
                if param.get("Type") == "String":
                    configurations[name] = param.get("Value")
                elif param.get("Type") == "StringList":
                    # Parse 'StringList' parameters into python lists
                    configurations[name] = param.get("Value").split(",")

            next_token = resp.get("NextToken")

    except Exception as e:
        print("Error fetching parameters from AWS")
        raise e


def setting(key: str, default_value=None, data_type: str = "str"):
    """
    Return a setting from an environment variable, AWS Parameter Store, or the default value if it's not set
    """

    # Note that the logic here will keep looking for the setting not just based on whether the key was found,
    # but also if the value itself is falsy. If the value is falsy, we'll keep falling back.

    # First, check if the setting is in the environment
    value = os.getenv(key, None)

    # Next, check if the setting is in the AWS parameter store, but only if the value wasn't found in env vars
    value = value or configurations.get(key, None)

    # Finally, return the default value if the setting wasn't found in either place
    value = value or default_value

    # Convert to the specified data type
    value = convert_value(value, data_type)

    return value


def convert_value(value, data_type):
    if data_type == "str":
        return value or ""

    if data_type == "int":
        return int(value)

    if data_type == "bool":
        if value.lower() == "true":
            return True

        return False

    if data_type == "list":
        if isinstance(value, str):
            value = value.split(",")
        value = [item.strip() for item in value]

        return value

    return value
