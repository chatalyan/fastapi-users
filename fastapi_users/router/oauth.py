from typing import Dict, List, Tuple, Union

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from httpx_oauth.integrations.fastapi import OAuth2AuthorizeCallback
from httpx_oauth.oauth2 import BaseOAuth2, OAuth2Token
from pydantic import BaseModel
from starlette.responses import RedirectResponse

from fastapi_users import models
from fastapi_users.authentication import AuthenticationBackend, Strategy
from fastapi_users.jwt import SecretType, decode_jwt, generate_jwt
from fastapi_users.manager import BaseUserManager, UserManagerDependency
from fastapi_users.router.common import ErrorCode, ErrorModel
from fastapi_users.settings import STATE_TOKEN_AUDIENCE, TOKEN_LIFETIME


class OAuth2AuthorizeResponse(BaseModel):
    authorization_url: str


def generate_state_token(
    data: Dict[str, str], secret: SecretType, lifetime_seconds: int = TOKEN_LIFETIME
) -> str:
    data["aud"] = STATE_TOKEN_AUDIENCE
    return generate_jwt(data, secret, lifetime_seconds)


def get_oauth_router(
    oauth_client: BaseOAuth2,
    backend: AuthenticationBackend,
    get_user_manager: UserManagerDependency[models.UC, models.UD],
    state_secret: SecretType,
    initial_redirect_url: str = None,
    initial_follow_redirect: bool = False,
) -> APIRouter:
    """Generate a router with the OAuth routes."""
    router = APIRouter()
    callback_route_name = f"oauth:{oauth_client.name}.{backend.name}.callback"

    if initial_redirect_url is not None:
        oauth2_authorize_callback = OAuth2AuthorizeCallback(
            oauth_client,
            redirect_url=initial_redirect_url,
        )
    else:
        oauth2_authorize_callback = OAuth2AuthorizeCallback(
            oauth_client,
            route_name=callback_route_name,
        )

    @router.get(
        "/authorize",
        name=f"oauth:{oauth_client.name}.{backend.name}.authorize",
        response_model=OAuth2AuthorizeResponse,
    )
    async def authorize(
        request: Request,
        redirect_url: str = None,
        follow_redirect: bool = False,
        scopes: List[str] = Query(None),
        code_challenge: str = None,
        code_challenge_method: str = None,
    ) -> Union[OAuth2AuthorizeResponse, RedirectResponse]:
        if redirect_url is not None:
            authorize_redirect_url = redirect_url
        elif initial_redirect_url is not None:
            authorize_redirect_url = initial_redirect_url
        else:
            authorize_redirect_url = request.url_for(callback_route_name)

        state_data: Dict[str, str] = {}
        state = generate_state_token(state_data, state_secret)
        authorization_url = await oauth_client.get_authorization_url(
            authorize_redirect_url,
            state,
            scopes,
            extras_params={
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method
            },
        )

        if follow_redirect or initial_follow_redirect:
            return RedirectResponse(authorization_url)
        return OAuth2AuthorizeResponse(authorization_url=authorization_url)

    @router.get(
        "/callback",
        name=callback_route_name,
        description="The response varies based on the authentication backend used.",
        responses={
            status.HTTP_400_BAD_REQUEST: {
                "model": ErrorModel,
                "content": {
                    "application/json": {
                        "examples": {
                            "INVALID_STATE_TOKEN": {
                                "summary": "Invalid state token.",
                                "value": None,
                            },
                            ErrorCode.LOGIN_BAD_CREDENTIALS: {
                                "summary": "User is inactive.",
                                "value": {"detail": ErrorCode.LOGIN_BAD_CREDENTIALS},
                            },
                        }
                    }
                },
            },
        },
    )
    async def callback(
        request: Request,
        response: Response,
        access_token_state: Tuple[OAuth2Token, str] = Depends(
            oauth2_authorize_callback
        ),
        user_manager: BaseUserManager[models.UC, models.UD] = Depends(get_user_manager),
        strategy: Strategy[models.UC, models.UD] = Depends(backend.get_strategy),
    ):
        token, state = access_token_state
        account_id, account_email = await oauth_client.get_id_email(
            token["access_token"]
        )

        try:
            decode_jwt(state, state_secret, [STATE_TOKEN_AUDIENCE])
        except jwt.DecodeError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

        new_oauth_account = models.BaseOAuthAccount(
            oauth_name=oauth_client.name,
            access_token=token["access_token"],
            expires_at=token.get("expires_at"),
            refresh_token=token.get("refresh_token"),
            account_id=account_id,
            account_email=account_email,
        )

        user = await user_manager.oauth_callback(new_oauth_account, request)

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorCode.LOGIN_BAD_CREDENTIALS,
            )

        # Authenticate
        return await backend.login(strategy, user, response)

    return router
