"""Image catalog and Distrobuilder build endpoints for BlueAgent v2."""

from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import PlainTextResponse

from ..dependencies import get_image_service, require_ctfd_token
from ..models import ImageRecord
from ..services.images import (
    ImageAliasInvalidError,
    ImageInUseConflictError,
    ImageService,
    ImageStateConflictError,
    ImageYamlInvalidError,
    ImageYamlTooLargeError,
)

router = APIRouter(
    prefix="/images",
    tags=["images"],
    dependencies=[Depends(require_ctfd_token)],
)


@router.put("/{alias}", response_model=ImageRecord)
async def put_image(
    alias: str,
    file: Optional[UploadFile] = File(default=None, description="Distrobuilder YAML file or build context archive"),
    definition: Optional[UploadFile] = File(default=None, description="Alias for file parameter"),
    force: bool = Query(default=False, description="Force rebuild even if image already ready"),
    service: ImageService = Depends(get_image_service),
):
    """Creates or updates a Distrobuilder image specification under the given alias.

    Accepts either a single definition.yaml or a build context archive (.tar.gz / .zip)
    containing definition.yaml plus auxiliary files. If YAML hash matches and image is ready,
    returns 200 OK without rebuilding. If hash changed, build failed, or ?force=true, triggers
    background compilation and returns 202 Accepted.
    """
    upload = file or definition
    if not upload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File upload ('file' or 'definition') is required",
        )

    content_bytes = await upload.read()
    try:
        record, is_pending = await service.submit_image(
            content_bytes=content_bytes,
            alias=alias,
            force=force,
        )
    except ImageYamlTooLargeError as err:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(err),
        )
    except (ImageYamlInvalidError, ImageAliasInvalidError) as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        )
    except ImageStateConflictError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )

    if is_pending:
        return Response(
            content=record.model_dump_json(),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    return record


@router.get("", response_model=List[ImageRecord])
async def list_images(service: ImageService = Depends(get_image_service)):
    """Lists all registered distrobuilder images and their build states."""
    return await service.list_images()


@router.get("/{alias}", response_model=ImageRecord)
async def get_image(alias: str, service: ImageService = Depends(get_image_service)):
    """Retrieves metadata and status for a specific image alias."""
    img = await service.get_image(alias)
    if not img:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image alias '{alias}' not found",
        )
    return img


@router.get("/{alias}/logs", response_class=PlainTextResponse)
async def get_image_logs(
    alias: str,
    service: ImageService = Depends(get_image_service),
):
    """Retrieves real-time or completed build logs for an image alias."""
    logs = await service.get_image_logs(alias)
    if logs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image alias '{alias}' not found",
        )
    return PlainTextResponse(content=logs, media_type="text/plain")


@router.delete("/{alias}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    alias: str,
    service: ImageService = Depends(get_image_service),
):
    """Deletes an image from Incus and the database catalog."""
    try:
        deleted = await service.delete_image(alias)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Image alias '{alias}' not found",
            )
    except ImageInUseConflictError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
