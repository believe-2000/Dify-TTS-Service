
import os
import uuid
import base64
import json
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

API_KEY = os.getenv("VOLCENGINE_API_KEY")
RESOURCE_ID = os.getenv("VOLCENGINE_RESOURCE_ID", "seed-tts-2.0")
SPEAKER = os.getenv(
    "TTS_SPEAKER",
    "zh_female_wenroumama_uranus_bigtts"
)

TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

# AUDIO_DIR = Path("audio")
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "audio"))
AUDIO_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Dify TTS Service")

app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str | None = None
    format: str = "mp3"
    sample_rate: int = 24000
    speech_rate: int = 0


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tts")
async def create_tts(req: TTSRequest):
    if not API_KEY:
        raise HTTPException(500, "服务端没有配置 VOLCENGINE_API_KEY")

    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text 不能为空")

    if req.format != "mp3":
        raise HTTPException(400, "当前服务只支持 mp3")

    speaker = req.voice or SPEAKER
    request_id = str(uuid.uuid4())

    headers = {
        "X-Api-Key": API_KEY,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": request_id,
        "Content-Type": "application/json",
    }

    payload = {
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": "mp3",
                "sample_rate": req.sample_rate,
                "speech_rate": req.speech_rate,
            },
            "additions": json.dumps({
                "context_texts": [
                    "请像一位真实的电台女主播，在清晨自然地和熟悉的朋友聊天。声音温柔、亲切、松弛，有自然的语气起伏和轻微停顿。不要使用新闻播音腔，不要刻意甜美，不要每个字都用力。"
                ]
            }, ensure_ascii=False),
        }
    }

    audio_bytes = bytearray()
    log_id = None

    try:
        timeout = httpx.Timeout(120.0, connect=20.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                TTS_URL,
                headers=headers,
                json=payload,
            ) as response:

                log_id = response.headers.get("X-Tt-Logid")
                if response.status_code != 200:
                    detail = (await response.aread()).decode(
                        "utf-8", errors="replace"
                    )
                    raise HTTPException(
                        response.status_code,
                        f"火山引擎请求失败：{detail[:1000]}"
                    )

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    code = item.get("code")
                    code = item.get("code")

                    # 火山引擎：0 表示正常数据帧；
                    # 20000000 表示合成成功结束。
                    if code is not None:
                        try:
                            code = int(code)
                        except (TypeError, ValueError):
                            raise HTTPException(
                                502,
                                f"无法识别火山引擎状态码：{code}"
                            )

                        if code not in (0, 20000000):
                            raise HTTPException(
                                502,
                                f"火山引擎合成失败："
                                f"code={code}, message={item.get('message', '')}"
                            )

                    audio_base64 = item.get("data")
                    if audio_base64:
                        audio_bytes.extend(
                            base64.b64decode(audio_base64)
                        )

        if not audio_bytes:
            raise HTTPException(
                502,
                "火山引擎没有返回音频数据，请检查音色、权限和请求日志"
            )

        filename = f"{uuid.uuid4().hex}.mp3"
        filepath = AUDIO_DIR / filename
        filepath.write_bytes(audio_bytes)

        # 部署到服务器后，请将 PUBLIC_BASE_URL 设置为公网 HTTPS 地址
        public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        audio_url = f"{public_base}/audio/{filename}" if public_base else f"/audio/{filename}"

        return {
            "success": True,
            "audio_url": audio_url,
            "filename": filename,
            "format": "mp3",
            "sample_rate": req.sample_rate,
            "speaker": speaker,
            "log_id": log_id,
        }

    except HTTPException:
        raise
    except httpx.RequestError as exc:
        raise HTTPException(502, f"连接火山引擎失败：{str(exc)}")
    except Exception as exc:
        raise HTTPException(500, f"服务内部错误：{str(exc)}")
