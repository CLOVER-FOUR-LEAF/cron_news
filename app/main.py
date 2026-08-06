from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db, get_session
from app.models import Category
from app.crud.category import COLOR_POOL, DEFAULT_COLOR
from app.routers import news, category, config, db as db_router, agent as agent_router, scheduler as scheduler_router, model_configs as model_configs_router
from app.services import db_service
from app.services.scheduler import scheduler as scheduler_service

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CATEGORIES = ["默认", "科技", "军事", "政治", "经济", "文化", "热点"]

KNOWN_CAT_COLORS = {
    "默认": "#8a8690",
    "科技": "#d4a853",
    "军事": "#c75050",
    "政治": "#4a8fd4",
    "经济": "#4aad6a",
    "文化": "#c47ddc",
    "热点": "#e88b5a",
}


async def choose_startup_database():
    state = db_service.get_db_state()
    if state["mode"] == "standalone" and state["complete"]:
        try:
            ok, msg = await db_service.test_connection(state["config"])
            if not ok:
                raise RuntimeError(msg)
            from app.database import activate_engine

            activate_engine(db_service.build_url(state["config"]))
            print("[DB] 使用独立数据库")
        except Exception as e:
            print(f"[DB] 独立数据库连接失败，回退到系统数据库: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    await choose_startup_database()
    await init_db()
    await seed_categories()
    await migrate_legacy_configs()
    scheduler_service.start()
    yield
    scheduler_service.stop()
    print(f"Shutting down {settings.APP_NAME}")


async def migrate_legacy_configs():
    from app.services.model_configs import migrate_legacy_configs as _migrate

    async with get_session() as session:
        try:
            migrated = await _migrate(session)
            await session.commit()
            if migrated:
                print("[Config] 已从 env 迁移旧的模型配置到数据库")
        except Exception as e:
            print(f"[Config] 迁移旧模型配置失败: {e}")


async def seed_categories():
    import random

    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Category))
        cats = result.scalars().all()

        if not cats:
            for i, name in enumerate(DEFAULT_CATEGORIES):
                session.add(Category(
                    name=name,
                    sort_order=i,
                    is_default=(name == "默认"),
                    color=KNOWN_CAT_COLORS.get(name, DEFAULT_COLOR),
                ))
            await session.commit()
            print(f"Seeded {len(DEFAULT_CATEGORIES)} default categories")
        else:
            used = {c.color for c in cats if c.color}
            changed = False
            for c in cats:
                if not c.color:
                    known = KNOWN_CAT_COLORS.get(c.name)
                    if known and known not in used:
                        c.color = known
                    else:
                        available = [x for x in COLOR_POOL if x not in used]
                        c.color = random.choice(available) if available else random.choice(COLOR_POOL)
                    used.add(c.color)
                    changed = True
            if changed:
                await session.commit()
                print("Backfilled missing category colors")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/images", StaticFiles(directory=BASE_DIR / "images"), name="images")

templates = Jinja2Templates(directory=BASE_DIR / "templates")

app.include_router(news.router)
app.include_router(category.router)
app.include_router(config.router)
app.include_router(db_router.router)
app.include_router(agent_router.router)
app.include_router(scheduler_router.router)
app.include_router(model_configs_router.router)


async def get_categories():
    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Category)
            .order_by(Category.sort_order.desc(), Category.id)
        )
        cats = result.scalars().all()
        names = [c.name for c in cats]
        colors = {c.name: (c.color or DEFAULT_COLOR) for c in cats}
        return names, colors


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


@app.get("/")
async def home(request: Request):
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(request, "home.html", {"categories": categories, "cat_colors": cat_colors, "active_page": "home"})


@app.get("/category/{category}")
async def category_page(request: Request, category: str):
    categories, cat_colors = await get_categories()
    if category not in categories:
        return templates.TemplateResponse(request, "404.html", status_code=404)
    return templates.TemplateResponse(request, "category.html", {"category": category, "categories": categories, "cat_colors": cat_colors, "active_page": "category", "active_category": category})


@app.get("/news/{news_id}")
async def news_detail(request: Request, news_id: int):
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(request, "news_detail.html", {"news_id": news_id, "categories": categories, "cat_colors": cat_colors, "active_page": "news"})


@app.get("/stats")
async def stats_page(request: Request):
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(request, "stats.html", {"categories": categories, "cat_colors": cat_colors, "active_page": "stats"})


@app.get("/brief")
async def brief_page(request: Request):
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(request, "brief.html", {"categories": categories, "cat_colors": cat_colors, "active_page": "brief"})


MY_PAGE_KINDS = {"recommend": "推荐阅读", "favorites": "新闻收藏", "later": "稍后再读"}


@app.get("/my/{kind}")
async def my_page(request: Request, kind: str):
    if kind not in MY_PAGE_KINDS:
        categories, cat_colors = await get_categories()
        return templates.TemplateResponse(request, "404.html", status_code=404, context={"categories": categories, "cat_colors": cat_colors})
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(
        request, "my.html",
        {"categories": categories, "cat_colors": cat_colors, "active_page": "my", "kind": kind, "kind_title": MY_PAGE_KINDS[kind]},
    )


@app.get("/search")
async def search_page(request: Request):
    categories, cat_colors = await get_categories()
    return templates.TemplateResponse(request, "search.html", {"categories": categories, "cat_colors": cat_colors, "active_page": "search"})
