import sys
import argparse
import yaml

def list_to_map(data):
    """Converts extraEnvs list to envMap dictionary."""
    if 'extraEnvs' in data:
        if isinstance(data['extraEnvs'], list):
            data['envMap'] = {item['name']: item['value'] for item in data['extraEnvs']}
        else:
            data['envMap'] = {}
        del data['extraEnvs']
    return data

def map_to_list(data):
    """Converts envMap dictionary to extraEnvs list."""
    if 'envMap' in data:
        if isinstance(data['envMap'], dict):
            data['extraEnvs'] = [{'name': k, 'value': v} for k, v in data['envMap'].items()]
        else:
            data['extraEnvs'] = []
        del data['envMap']
    return data

def main():
    parser = argparse.ArgumentParser(description="Convert Helm YAML lists to maps and vice versa.")
    parser.add_argument('-r', '--reverse', action='store_true', help="Convert maps back to lists")
    parser.add_argument('input_file', help="Path to the input YAML file")
    
    args = parser.parse_args()

    try:
        with open(args.input_file, 'r') as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"Error: File '{args.input_file}' not found.", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML: {e}", file=sys.stderr)
        sys.exit(1)

    if args.reverse:
        transformed_data = map_to_list(data)
    else:
        transformed_data = list_to_map(data)

    # Output to stdout
    yaml.dump(transformed_data, sys.stdout, default_flow_style=False, sort_keys=False)

if __name__ == '__main__':
    main()
