def get_tv_provider(source):
    from app.models import Sources
    from . import tmdb, tvdb

    providers = {
        Sources.TMDB: tmdb,
        Sources.TVDB: tvdb,
    }

    try:
        return providers[Sources(source)]
    except KeyError:
        raise ValueError(f"Unsupported TV provider source: {source}")
