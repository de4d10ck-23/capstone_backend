import uuid
from core.database import get_supabase
from core.config import settings


async def upload_to_supabase(file_bytes: bytes, filename: str, content_type: str) -> str:
    """Upload a file to the Supabase Storage bucket and return the public URL."""
    sb = get_supabase()
    bucket = settings.SUPABASE_STORAGE_BUCKET

    # Generate a unique path to avoid collisions
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    path = f"water-locations/{unique_name}"

    # Upload the file
    sb.storage.from_(bucket).upload(
        path,
        file_bytes,
        file_options={"content-type": content_type},
    )

    # Get the public URL
    public_url = sb.storage.from_(bucket).get_public_url(path)
    return public_url


async def delete_from_supabase(file_url: str) -> bool:
    """Delete a file from Supabase Storage by its public URL. Returns True on success."""
    sb = get_supabase()
    bucket = settings.SUPABASE_STORAGE_BUCKET

    try:
        # Extract the path from the public URL
        # URL format: .../storage/v1/object/public/<bucket>/<path>
        parts = file_url.split(f"/storage/v1/object/public/{bucket}/")
        if len(parts) != 2:
            return False
        path = parts[1]
        sb.storage.from_(bucket).remove([path])
        return True
    except Exception:
        return False
