from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Coroutine, TypeVar, Union, reveal_type

from aiohttp import ClientResponse, ClientSession
from playwright import async_api as playwright

from . import __version__
from .chatroom import ChatroomWebSocket
from .errors import Forbidden, HTTPException, InternalKickException, NotFound
from .utils import MISSING

if TYPE_CHECKING:
    from .client import Client
    from .types.message import MessageSentPayload
    from .types.user import UserPayload

    T = TypeVar("T")
    Response = Coroutine[Any, Any, T]

KickResponse = ClientResponse | playwright.Response | playwright.APIResponse

LOGGER = logging.getLogger(__name__)

NOTFOUND_SIGNATURE = """
class="w-64 lg:w-[526px]"
""".strip()


async def json_or_text(response: KickResponse, /) -> Union[dict[str, Any], str]:
    text = await response.text()
    try:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    except KeyError:
        pass

    return text


async def error_or_text(data: Union[dict, str]) -> str:
    if isinstance(data, dict):
        return data["error"]
    else:
        return data


class Route:
    DOMAIN: str = "https://kick.com"
    BASE: str = f"{DOMAIN}/api/v2"

    def __init__(
        self,
        method: str,
        path: str,
    ) -> None:
        self.path: str = path
        self.method: str = method
        self.url = self.BASE + self.path


class HTTPClient:
    def __init__(self, client: Client):
        self.__session: ClientSession = MISSING
        self.__browser: playwright.ChromiumBrowserContext = MISSING
        self.__chromium: playwright.Browser = MISSING
        self.ws: ChatroomWebSocket = MISSING
        self.client = client

        self.token: str = MISSING
        self.xsrf_token: str = MISSING

    async def close(self) -> None:
        print("Closing HTTPClient...")
        if self.__browser is not MISSING:
            await self.__browser.close()
        if self.__session is not MISSING:
            await self.__session.close()
        if self.ws is not MISSING:
            await self.ws.close()

    async def populate_browser(self):
        if self.__browser is MISSING:
            pw = await playwright.async_playwright().start()
            self.__chromium = await pw.chromium.launch(headless=False)
            self.__browser = await self.__chromium.new_context()

    async def login(self, username: str, password: str) -> None:
        await self.populate_browser()

        page = await self.__browser.new_page()
        url = Route.DOMAIN
        headers = {}
        # headers["host"] = "kick.com"
        await page.set_extra_http_headers(headers)
        res = await page.goto(url)
        if res:
            with open("content.html", "w") as f:
                f.write(await res.text())
        await page.click("#login-button")
        await page.type("#email", username)
        await page.type("#password", password)
        await page.click("#signin-modal button[type=submit]")
        # await page.close()
        print("Logged in.")
        await self.update_tokens()

    async def update_tokens(self) -> None:
        cookies = await self.__browser.cookies()
        with open("cookies.json", "w") as f:
            json.dump(cookies, f, indent=4)
        for cookie in cookies:
            value = cookie.get("value")
            if value is not None:
                match cookie.get("name"):
                    case "kick_session":
                        self.token = value
                    case "XSRF-TOKEN":
                        self.xsrf_token = value

    async def start(self) -> None:
        if self.__session is MISSING:
            self.__session = ClientSession()

        actual_ws = await self.__session.ws_connect(
            f"wss://ws-us2.pusher.com/app/eb1d5f283081a78b932c?protocol=7&client=js&version=7.6.0&flash=false"
        )
        self.ws = ChatroomWebSocket(actual_ws, http=self)
        self.client.dispatch("ready")
        print("dispatched ready")
        await self.ws.start()

    async def request(self, route: Route, **kwargs) -> Any:
        await self.populate_browser()

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["referrer"] = "kick.com"
        headers["Connection"] = "keep-alive"
        headers["Alt-Used"] = "kick.com"
        # headers["Host"] = "kick.com"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Sec-Fetch-User"] = "?1"
        headers["Sec-GPC"] = "1"
        # headers["TE"] = "trailers"
        headers["X-XSRF-TOKEN"] = self.xsrf_token
        headers["Accept-Language"] = "en-CA,en-US;q=0.7,en;q=0.3"
        headers["Accept-Encoding"] = "gzip, deflate, br"
        headers[
            "Accept"
        ] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

        url = route.url
        endpoint = f"/{route.method.split('/')[-1]}"

        if "json" in kwargs:
            headers["Content-Type"] = "application/json"

        res: KickResponse | None = None
        data: str | dict | None = None
        page: playwright.Page | None = None
        # try:
        for current_try in range(3):
            LOGGER.debug(
                f"Making request to {url}. headers: {headers}, params: {kwargs.get('params', None)}, json: {kwargs.get('json', None)}"
            )
            page = await self.__browser.new_page()
            print("new page made")
            match route.method:
                case "GET":
                    print("making get...")
                    await page.set_extra_http_headers(headers)
                    print("Headers set")
                    res = await page.goto(url)
                    print("get request made")
                case "POST":
                    print("making post...")
                    res = await self.__browser.request.post(
                        url, headers=headers, **kwargs
                    )
                    print("post request made")
                case other:
                    raise NotImplementedError(
                        f"Implimentation for the {route.method} http method is not set"
                    )
            print("done working w/ methods")
            if res is not None:
                print("response found")
                data = await json_or_text(res)
                with open("data", "w") as f:
                    f.write(f"{data}")

                LOGGER.debug(
                    f"Received Response w/ code {res.status}. headers: {res.headers}, data: {data}"
                )
                if NOTFOUND_SIGNATURE in data:
                    error = await error_or_text(data)
                    raise NotFound(error)

                if 300 > res.status >= 200:
                    print("returning data...")
                    return data
                match res.status:
                    case 400:
                        error = await error_or_text(data)
                        raise HTTPException(error)
                    case 403:
                        raise Forbidden()
                    case 404:
                        error = await error_or_text(data)
                        raise NotFound(error)
                    case 429:
                        LOGGER.warning(
                            "We have been ratelimited. Waiting five seconds before trying again...",
                            endpoint,
                        )
                        await asyncio.sleep(5)
                        return await self.request(route)
                    case 500:
                        time = 2 * current_try

                        LOGGER.warning(
                            "API returned a 500 status code at '%s'. Retrying in %s seconds",
                            endpoint,
                        )
                        await asyncio.sleep(time)
                        continue
                    case 502:
                        txt = await error_or_text(data)
                        raise InternalKickException(txt)
                    case other:
                        raise RuntimeError(f"Unknown status reached: {other}")
        # finally:
        #     print("finally triggered")
        #     if page is not None:
        #         print("Closing page...")
        #         await page.close()

        if res is not None and data is not None:
            txt = await error_or_text(data)

            if res.status >= 500:
                raise InternalKickException(txt)

            raise HTTPException(txt)

        raise RuntimeError(
            f"Unreachable situation occured in http handling. Res: {res}, data: {data}"
        )
        # raise RuntimeError("Unreachable situation occured in http handling")

    def send_message(self, chatroom: int, content: str) -> Response[MessageSentPayload]:
        return self.request(
            Route(method="POST", path=f"/messages/send/{chatroom}"),
            json={"content": content, "type": "message"},
        )

    def get_user(self, streamer: str) -> Response[UserPayload]:
        return self.request(Route(method="GET", path=f"/channels/{streamer}"))
