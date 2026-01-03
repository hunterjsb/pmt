#!/bin/bash
# Generate Python protobuf files from proto schema
# Run from proxy/client directory

set -e

PROTO_DIR="../proto"
OUT_DIR="."

echo "Generating Python protobuf files..."
python -m grpc_tools.protoc \
    -I"$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_DIR/polymarket.proto"

# Fix the import in the generated grpc file
sed -i '' 's/import polymarket_pb2/from . import polymarket_pb2/' polymarket_pb2_grpc.py 2>/dev/null || \
sed -i 's/import polymarket_pb2/from . import polymarket_pb2/' polymarket_pb2_grpc.py

echo "Done! Generated:"
ls -la polymarket_pb2*.py
