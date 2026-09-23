FROM python:3.12-slim
WORKDIR /research
COPY research_dataset_v2.py /research/research_dataset_v2.py
COPY research_dataset_v3.py /research/research_dataset_v3.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 DATA_ROOT=/data/bgx-missed-market-002 BGX_LOG_CHUNK_SIZE=20000
EXPOSE 8080
CMD ["python", "-u", "/research/research_dataset_v3.py"]
