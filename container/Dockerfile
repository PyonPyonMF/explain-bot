FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core blender xvfb xauth libgl1-mesa-dri \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Blender provides bpy itself. Install only the pure-Python VRM add-on, not a second bpy runtime.
RUN pip install --no-cache-dir --no-deps --target /opt/blender-addons bpy-vrm-format==4.9.1

ENV MPLBACKEND=Agg PYTHONUNBUFFERED=1 MPLCONFIGDIR=/app/.mpl
# Build the matplotlib font cache now, not on the first job.
RUN python -c "import matplotlib.pyplot as plt; plt.figure(); plt.text(0, 0, 'тест \$x^2\$'); plt.savefig('/tmp/t.png')"

# Generated animation code runs as this user, without the API keys (see fullgen.py / gen_harness.py).
RUN useradd --create-home --uid 10001 sandbox \
 && su sandbox -s /bin/sh -c "MPLCONFIGDIR=/home/sandbox/.mpl python -c \"import matplotlib.pyplot as plt; plt.figure(); plt.text(0, 0, 'тест'); plt.savefig('/tmp/s.png')\""

COPY plan.py scenes.py pipeline.py inputs.py assets.py server.py kit.py gen_harness.py fullgen.py interactions.py gateway.py render_modes.py three_d.py three_d_kit.py three_d_runtime.py three_d_library_export.py object_library.py remote_render.py commands.json ./
RUN chmod -R a+rX /app

EXPOSE 8080
CMD ["python", "server.py"]
