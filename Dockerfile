FROM twmvmirz/py3.12-rocksdb8.1.1:latest

# Workaround for RocksDB memory leak
# (https://blog.cloudflare.com/the-effect-of-switching-to-tcmalloc-on-rocksdb-memory-use/)
ENV MALLOC_ARENA_MAX=2

RUN mkdir -p /app
WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

CMD ["python", "-m", "kaspr", "worker", "-l", "info"]