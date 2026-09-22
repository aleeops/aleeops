import sys
import argparse
import yaml

def list_to_map(data):
    """Converts extraEnvs list to envMap dictionary, preserving complex structures like valueFrom."""
    if 'extraEnvs' in data:
        if isinstance(data['extraEnvs'], list):
            data['envMap'] = {}
            for item in data['extraEnvs']:
                name = item.get('name')
                if not name:
                    continue

                # If it's a simple name/value pair, map directly to the value string
                if 'value' in item and len(item) == 2:
                    data['envMap'][name] = item['value']
                else:
                    # For valueFrom or other complex attributes, map the rest of the dictionary
                    data['envMap'][name] = {k: v for k, v in item.items() if k != 'name'}
        else:
            data['envMap'] = {}
        del data['extraEnvs']
    return data

def map_to_list(data):
    """Converts envMap dictionary back to extraEnvs list."""
    if 'envMap' in data:
        if isinstance(data['envMap'], dict):
            data['extraEnvs'] = []
            for k, v in data['envMap'].items():
                item = {'name': k}
                # If the value is a dictionary (like valueFrom), merge it into the item
                if isinstance(v, dict):
                    item.update(v)
                else:
                    item['value'] = v
                data['extraEnvs'].append(item)
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