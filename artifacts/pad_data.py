import csv
import sys

# Read the CSV file
input_file = "llmiter_rlt_100.csv"
data = []

try:
    with open(input_file, 'r') as file:
        csv_reader = csv.reader(file)
        for row in csv_reader:
            data.extend(map(float, row))
except FileNotFoundError:
    print(f"Error: File '{input_file}' not found.", file=sys.stderr)
    sys.exit(1)
except csv.Error as e:
    print(f"Error reading CSV file: {e}", file=sys.stderr)
    sys.exit(1)

# Calculate the number of zeros to pad
padding_count = max(0, 444 - len(data))

# Pad the data with zeros
padded_data = data + [0.0] * padding_count

# Write the padded data to stdout
csv_writer = csv.writer(sys.stdout)
csv_writer.writerow(padded_data)