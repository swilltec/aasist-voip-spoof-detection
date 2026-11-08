FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /opt/aasist

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
    && pip install --no-cache-dir -r requirements.txt

ARG AASIST_REVISION=a04c9863f63d44471dde8a6abcb3b082b07cd1d1
ADD https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/models/AASIST.py ./models/AASIST.py
ADD https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/data_utils.py ./data_utils.py
ADD https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/config/AASIST.conf ./config/AASIST.conf
ADD --checksum=sha256:51d2d9cf0738172f61e2a384ec50a54a55363240f67c971ed55a92435bc1a1c0 https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/models/weights/AASIST.pth ./models/weights/AASIST.pth
ADD https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/LICENSE ./LICENSE
ADD https://raw.githubusercontent.com/clovaai/aasist/${AASIST_REVISION}/NOTICE ./NOTICE

COPY app.py admin.py ./

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
