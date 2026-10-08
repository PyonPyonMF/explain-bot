FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV MPLBACKEND=Agg PYTHONUNBUFFERED=1 MPLCONFIGDIR=/app/.mpl
# Build the matplotlib font cache now, not on the first job.
RUN python -c "import matplotlib.pyplot as plt; plt.figure(); plt.text(0, 0, 'тест \$x^2\$'); plt.savefig('/tmp/t.png')"

COPY plan.py scenes.py pipeline.py inputs.py server.py ./

EXPOSE 8080
CMD ["python", "server.py"]
