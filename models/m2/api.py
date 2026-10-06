from fastapi import FastAPI, HTTPException

from .service import (
    M2ConfigurationError,
    NoDomainAnswersError,
    PermanentLLMError,
    TransientLLMError,
    M2Merger,
)
from .schemas import M2Input, M2Response


def create_app(merger: M2Merger | None = None) -> FastAPI:
    service = merger or M2Merger()
    app = FastAPI(title="Cortex M2", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "cortex-m2"}

    @app.post("/merge", response_model=M2Response)
    async def merge(payload: M2Input) -> M2Response:
        try:
            return await service.merge(payload)
        except NoDomainAnswersError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except M2ConfigurationError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except TransientLLMError as error:
            raise HTTPException(status_code=503, detail="M2 model is temporarily unavailable") from error
        except PermanentLLMError as error:
            raise HTTPException(status_code=502, detail="M2 model request was rejected") from error

    return app


app = create_app()
