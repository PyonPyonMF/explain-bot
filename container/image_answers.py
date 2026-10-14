"""Standalone raster answers through the existing private Codex built-in ImageGen worker."""
import base64
import io
import json
import logging
import os
from pathlib import Path
import uuid

from PIL import Image

log=logging.getLogger("image-answers")


def make_image(job,workdir):
    import pipeline as P
    import assets
    from followups import prepare
    if os.environ.get("CODEX_IMAGES","1")!="1":
        raise P.UserError("Генерация картинок сейчас отключена." if P.BOT_LANG=="ru" else "Image generation is disabled.")
    job["context_messages"]=0
    prepared=prepare(job,P.collect)
    job["prepared_inputs"]=prepared
    images,texts,_=prepared
    request=str(job.get("question") or job.get("text") or "")
    from render_modes import _request_words
    bare=_request_words(request).strip(" .!?,")
    empty=bare in ("сгенерируй картинку","создай картинку","сделай картинку","сгенерируй изображение",
                  "нарисуй","generate an image","generate a picture","create an image","draw")
    history=(job.get("followup") or {}).get("history") or []
    if empty and not images and not texts and not history:
        raise P.UserError("Опиши, что нарисовать, или ответь на сообщение с нужной темой и моим упоминанием." if P.BOT_LANG=="ru" else
                          "Describe what to draw, or reply to the source with my mention.")
    context="\n".join(texts)
    if job.get("followup"):
        from followups import prompt
        context=prompt(job)+"\n"+context
    elif job.get("kind")=="message":
        context="Quoted source to illustrate:\n"+str((job.get("message") or {}).get("content") or "")+"\n"+context
    references=[]
    originals=job.get("image_originals") or []
    for i,block in enumerate(images[:2]):
        data=originals[i] if i<len(originals) and isinstance(originals[i],bytes) else base64.b64decode(block["source"]["data"],validate=True)
        path=Path(workdir)/f"image-reference-{i}.png"
        with Image.open(io.BytesIO(data)) as reference:
            reference.load()
            reference.thumbnail((2048,2048),Image.Resampling.LANCZOS)
            reference.convert("RGBA" if reference.mode in ("RGBA","LA") or "transparency" in reference.info else "RGB").save(path,"PNG")
        references.append({"path":str(path)})
    try:
        data,result=assets.request_codex(request,"",references,timeout=750,mode="image",source_context=context)
        if not data:
            raise RuntimeError("no_image")
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        log.warning("Codex standalone image unavailable: %s",type(exc).__name__)
        raise P.UserError(P.S["image_failed"]) from None
    return data,str(result.get("description") or "AI-изображение")[:500]


def output_png(data,limit):
    """Preserve alpha and reduce only when the Discord upload cap requires it."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        image=image.convert("RGBA" if image.mode in ("RGBA","LA") or "transparency" in image.info else "RGB")
        while True:
            out=io.BytesIO();image.save(out,"PNG",optimize=True)
            raw=out.getvalue()
            if len(raw)<=limit:return raw
            if min(image.size)<=256:raise ValueError("image_upload_limit")
            image.thumbnail((max(1,int(image.width*.8)),max(1,int(image.height*.8))),Image.Resampling.LANCZOS)


def deliver(job,data,description):
    import pipeline as P
    import followups
    try:raw=output_png(data,max(1,int(P.MAX_UPLOAD_MB*1024*1024)))
    except Exception:raise P.UserError(P.S["image_failed"]) from None
    boundary="image-"+uuid.uuid4().hex
    payload={"content":"🖼️ AI-изображение · Codex ImageGen","allowed_mentions":{"parse":[]},
             "attachments":[{"id":0,"filename":"image.png"}]}
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode()
          +json.dumps(payload).encode()+f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="image.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
          +raw+f'\r\n--{boundary}--\r\n'.encode())
    P._retry(lambda:P._http(P._discord_url(job),body,P._discord_headers(job,"multipart/form-data; boundary="+boundary),
                            method="PATCH",timeout=180),"discord image upload")
    try:
        followups.record_image(job,description)
        followups.remember(job)
    except Exception as exc:
        log.warning("could not save image memory: %s",type(exc).__name__)
