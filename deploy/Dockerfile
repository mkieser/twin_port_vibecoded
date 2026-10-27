# One image for everything in template.yaml: the AIS consumer on ECS Fargate and
# twin_app.py on EC2 (entry points set there), and compact_handler (this image's
# default) and the tide and storm surge poll_handlers on Lambda.
# Build from the repository root: docker build -f deploy/Dockerfile .
FROM public.ecr.aws/lambda/python:3.14

COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Lambda can't write extensions at run time, so install them now (see storage.py)
ENV DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb_extensions
RUN python -c "import duckdb; con = duckdb.connect(); \
con.execute(\"SET extension_directory = '$DUCKDB_EXTENSION_DIRECTORY'\"); \
[con.execute(f'INSTALL {e}') for e in ('spatial', 'httpfs', 'aws')]"

# load_rasters.DATA_DIR is the scripts folder's ../data
COPY data/extent.gpkg data/berth.gpkg /var/data/
COPY scripts/storage.py scripts/load_rasters.py scripts/ais_consumer.py \
     scripts/tide_consumer.py scripts/tide_live.py scripts/storm_surge_consumer.py \
     scripts/ais_live.py scripts/storm_surge_live.py scripts/twin_app.py \
     ${LAMBDA_TASK_ROOT}/
COPY scripts/twin_app/ ${LAMBDA_TASK_ROOT}/twin_app/

CMD ["ais_consumer.compact_handler"]
