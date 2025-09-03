from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from typing import Optional
import asyncio
import os
import tempfile
import uuid
from datetime import datetime
import traceback
import logging
from minio import Minio
from minio.error import S3Error
from tweetcapture import TweetCapture
from PIL import Image, ImageOps
from collections import Counter

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="TweetCapture API",
    description="API for capturing tweet screenshots",
    version="1.0.0"
)

# ------------------ Pydantic Models ------------------
class TweetCaptureRequest(BaseModel):
    url: str = Field(..., description="Tweet URL")
    mode: int = Field(default=3, ge=0, le=4, description="Display mode (0-4)")
    night_mode: int = Field(default=0, ge=0, le=2, description="Night mode theme (0-2)")
    lang: str = Field(default="en", description="Browser language code")
    show_parent_tweets: bool = Field(default=False, description="Show parent tweets")
    show_parent_limit: int = Field(default=-1, description="Parent tweets limit (-1 = unlimited)")
    show_mentions: int = Field(default=0, description="Show mentions count")
    radius: int = Field(default=15, description="Image radius")
    scale: float = Field(default=1.0, ge=0.1, le=14.0, description="Screenshot scale")
    wait_time: float = Field(default=5.0, ge=1.0, le=10.0, description="Page loading wait time")

    # Media hiding options
    hide_photos: bool = Field(default=False)
    hide_videos: bool = Field(default=False)
    hide_gifs: bool = Field(default=False)
    hide_quotes: bool = Field(default=False)
    hide_link_previews: bool = Field(default=False)
    hide_all_medias: bool = Field(default=False)

    # Optional custom filename (without extension)
    filename: Optional[str] = None

    # Crop amount for non-media tweets
    crop_top: int = Field(default=10, ge=0, description="Pixels to crop from the top for non-media tweets")

    @validator('url')
    def validate_tweet_url(cls, v):
        if not v.startswith(('https://twitter.com/', 'https://x.com/')):
            raise ValueError('URL must be a valid Twitter/X URL')
        return v

class TweetCaptureResponse(BaseModel):
    success: bool
    message: str
    file_url: Optional[str] = None
    filename: Optional[str] = None
    file_size: Optional[int] = None
    processing_time: Optional[float] = None

class HealthResponse(BaseModel):
    status: str
    timestamp: datetime

# ------------------ MinIO Configuration ------------------
MINIO_ENDPOINT = os.getenv('MINIO_ENDPOINT', 'localhost:9000')
MINIO_ACCESS_KEY = os.getenv('MINIO_ACCESS_KEY', 'minioadmin')
MINIO_SECRET_KEY = os.getenv('MINIO_SECRET_KEY', 'minioadmin')
MINIO_BUCKET = os.getenv('MINIO_BUCKET', 'tweetcaptures')
MINIO_SECURE = os.getenv('MINIO_SECURE', 'false').lower() == 'true'
MINIO_PUBLIC_ENDPOINT = os.getenv('MINIO_PUBLIC_ENDPOINT', f"{'https' if MINIO_SECURE else 'http'}://{MINIO_ENDPOINT}")

minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=MINIO_SECURE
)

def ensure_bucket_exists():
    try:
        if not minio_client.bucket_exists(MINIO_BUCKET):
            minio_client.make_bucket(MINIO_BUCKET)
            logger.info(f"Created bucket: {MINIO_BUCKET}")
    except S3Error as e:
        logger.error(f"Error with MinIO bucket: {e}")
        raise

def upload_to_minio(file_path: str, object_name: str) -> str:
    try:
        minio_client.fput_object(MINIO_BUCKET, object_name, file_path)
        return f"{MINIO_PUBLIC_ENDPOINT}/{MINIO_BUCKET}/{object_name}"
    except S3Error as e:
        logger.error(f"Error uploading to MinIO: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")

def generate_filename(tweet_url: str, custom_filename: Optional[str] = None) -> str:
    import re
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if custom_filename:
        return f"{custom_filename}_{timestamp}_{str(uuid.uuid4())[:8]}.png"
    match = re.search(r'\/(\w+)\/status\/(\d+)', tweet_url)
    if match:
        username, tweet_id = match.groups()
        return f"@{username}_{tweet_id}_{timestamp}.png"
    return f"tweet_{timestamp}_{str(uuid.uuid4())[:8]}.png"

async def cleanup_temp_file(file_path: str):
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning(f"Failed to clean up {file_path}: {e}")

# ------------------ Image Processing ------------------
def get_dominant_color(image: Image.Image) -> tuple:
    img = image.convert("RGB")
    pixels = list(img.getdata())
    most_common = Counter(pixels).most_common(1)[0][0]
    return most_common

def process_for_instagram(
    image_path: str,
    output_path: str,
    size_w: int = 1080,
    size_h: int = 1350,
    has_media: bool = False,
    crop_top: int = 10
):
    """
    Process image for Instagram:
    - Resize all tweets (media & non-media) to fit into 4:5 ratio (1080x1350).
    - Non-media tweets are cropped from the top by crop_top pixels before resizing.
    - Background filled with dominant color when padding is needed.
    """
    img = Image.open(image_path).convert("RGBA")

    if has_media:
        # Resize with padding to 1080x1350
        fg_fit = ImageOps.contain(img, (size_w, size_h), Image.LANCZOS)
        dominant_color = get_dominant_color(img)
        bg = Image.new("RGBA", (size_w, size_h), dominant_color + (255,))

        fg_w, fg_h = fg_fit.size
        off = ((size_w - fg_w) // 2, (size_h - fg_h) // 2)
        bg.paste(fg_fit, off, mask=fg_fit.split()[3] if fg_fit.mode == "RGBA" else None)

        bg.save(output_path, "PNG", optimize=True, dpi=(300, 300))
        return output_path

    # ---- Non-media tweets ----
    w, h = img.size
    crop_box = (0, crop_top, w, h) if crop_top < h else (0, 0, w, h)
    cropped = img.crop(crop_box)

    # Resize with padding to 1080x1350
    fg_fit = ImageOps.contain(cropped, (size_w, size_h), Image.LANCZOS)
    dominant_color = get_dominant_color(cropped)
    bg = Image.new("RGBA", (size_w, size_h), dominant_color + (255,))

    fg_w, fg_h = fg_fit.size
    off = ((size_w - fg_w) // 2, (size_h - fg_h) // 2)
    bg.paste(fg_fit, off, mask=fg_fit.split()[3] if fg_fit.mode == "RGBA" else None)

    bg.save(output_path, "PNG", optimize=True, dpi=(300, 300))
    return output_path

# ------------------ FastAPI Events ------------------
@app.on_event("startup")
async def startup_event():
    ensure_bucket_exists()
    logger.info("TweetCapture API started")

@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="healthy", timestamp=datetime.now())

# ------------------ Main Capture Endpoint ------------------
@app.post("/capture", response_model=TweetCaptureResponse)
async def capture_tweet(request: TweetCaptureRequest, background_tasks: BackgroundTasks):
    start_time = datetime.now()
    temp_file_path = None
    final_file_path = None
    
    try:
        object_name = generate_filename(request.url, request.filename)

        temp_dir = tempfile.gettempdir()
        temp_file_path = os.path.join(temp_dir, f"temp_{uuid.uuid4().hex}.png")

        # Capture Tweet
        tweet_capture = TweetCapture(
            mode=request.mode,
            night_mode=request.night_mode,
            test=False,
            show_parent_tweets=request.show_parent_tweets,
            parent_tweets_limit=request.show_parent_limit,
            show_mentions_count=request.show_mentions,
            overwrite=True,
            radius=request.radius,
            scale=request.scale
        )
        tweet_capture.set_lang(request.lang)
        tweet_capture.set_wait_time(request.wait_time)

        if request.hide_all_medias:
            tweet_capture.hide_all_media()
        else:
            tweet_capture.hide_media(
                link_previews=request.hide_link_previews,
                photos=request.hide_photos,
                videos=request.hide_videos,
                gifs=request.hide_gifs,
                quotes=request.hide_quotes
            )

        logger.info(f"Capturing screenshot: {request.url}")
        result_path = await tweet_capture.screenshot(request.url, temp_file_path)

        # Decide if tweet has media
        has_media = not request.hide_all_medias and (
            not request.hide_photos or
            not request.hide_videos or
            not request.hide_gifs or
            not request.hide_quotes or
            not request.hide_link_previews
        )

        final_file_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}.png")
        process_for_instagram(
            result_path,
            final_file_path,
            size_w=1080,
            size_h=1350,
            has_media=has_media,
            crop_top=request.crop_top
        )

        # Upload to MinIO
        file_url = upload_to_minio(final_file_path, object_name)
        file_size = os.path.getsize(final_file_path)

        # Cleanup
        background_tasks.add_task(cleanup_temp_file, temp_file_path)
        background_tasks.add_task(cleanup_temp_file, final_file_path)

        processing_time = (datetime.now() - start_time).total_seconds()
        return TweetCaptureResponse(
            success=True,
            message="Tweet captured successfully",
            file_url=file_url.replace("storage", "storage-api"),
            filename=object_name,
            file_size=file_size,
            processing_time=processing_time
        )

    except Exception as e:
        if temp_file_path:
            background_tasks.add_task(cleanup_temp_file, temp_file_path)
        if final_file_path:
            background_tasks.add_task(cleanup_temp_file, final_file_path)
        logger.error(traceback.format_exc())
        return TweetCaptureResponse(
            success=False,
            message=f"Error: {str(e)}",
            processing_time=(datetime.now() - start_time).total_seconds()
        )

@app.get("/")
async def root():
    return {
        "name": "TweetCapture API",
        "version": "1.0.0",
        "description": "Capture tweets, adjust for Instagram, and upload to MinIO",
        "endpoints": {
            "capture": "POST /capture",
            "health": "GET /health",
            "docs": "GET /docs"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
