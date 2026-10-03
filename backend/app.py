import os
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
from sqlalchemy import Boolean, ForeignKey, Integer, JSON, Numeric, String, create_engine, delete, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

load_dotenv()


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
RAW_IMAGE_DIR = BACKEND_DIR / "data" / "raw"
ATTACHED_ASSET_DIR = PROJECT_DIR / "attached_assets"
DEFAULT_DATABASE_URL = f"sqlite:///{(BACKEND_DIR / 'data' / 'safarz.db').as_posix()}"
GEOCODE_CACHE = {}
ROUTE_CACHE = {}


class Base(DeclarativeBase):
    pass


class Stop(Base):
    __tablename__ = "stops"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_ur: Mapped[str | None] = mapped_column(String(160))
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    latitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    route_stops: Mapped[list["RouteStop"]] = relationship(back_populates="stop")


class Bus(Base):
    __tablename__ = "buses"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    route_code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False)
    has_ac: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_wheelchair: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_filename: Mapped[str | None] = mapped_column(String(255))
    source_image: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(40), default="other", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    routes: Mapped[list["Route"]] = relationship(back_populates="bus")


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    bus_id: Mapped[str] = mapped_column(ForeignKey("buses.id"), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    frequency_minutes: Mapped[int | None] = mapped_column(Integer)
    fare_label: Mapped[str | None] = mapped_column(String(120))
    fare_kind: Mapped[str] = mapped_column(String(20), default="variable", nullable=False)
    geometry: Mapped[dict | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    bus: Mapped[Bus] = relationship(back_populates="routes")
    route_stops: Mapped[list["RouteStop"]] = relationship(back_populates="route", order_by="RouteStop.stop_order")


class RouteStop(Base):
    __tablename__ = "route_stops"

    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"), primary_key=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"), primary_key=True)
    stop_order: Mapped[int] = mapped_column(Integer, nullable=False)
    route: Mapped[Route] = relationship(back_populates="route_stops")
    stop: Mapped[Stop] = relationship(back_populates="route_stops")


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    database_url = database_url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    engine = create_engine(database_url, future=True)
    upgrade_schema(engine)
    Base.metadata.create_all(engine)

    if os.getenv("SEED_DATABASE", "1") == "1":
        seed_database(engine)

    app.config["DATABASE_ENGINE"] = engine

    @app.after_request
    def add_api_headers(response):
        if request.path.startswith("/api/") or request.path.startswith("/media/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, databaseConfigured=bool(os.getenv("DATABASE_URL")))

    @app.get("/api/config")
    def config():
        return jsonify(
            mapsProvider=os.getenv("MAPS_PROVIDER", "custom"),
            mapsPublicKey=os.getenv("MAPS_PUBLIC_KEY"),
            aiProvider="groq" if os.getenv("GROQ_API_KEY") else "database",
            aiModel=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        )

    @app.get("/api/stops")
    def stops():
        query = request.args.get("q", "").strip()
        query = {"sadar": "saddar", "nagan": "nagan chorangi", "nipa": "nipa chowrangi"}.get(query.lower(), query)
        with Session(engine) as session:
            candidates = session.scalars(select(Stop).order_by(Stop.name)).all()
            if query:
                candidates = [
                    stop for stop in candidates
                    if query.lower() in " ".join(stop.aliases or []).lower()
                    or query.lower() in stop.name.lower()
                    or query in (stop.name_ur or "")
                ]
            candidates = candidates[:20]
            return jsonify(stops=[stop_json(stop) for stop in candidates])

    @app.get("/api/routes")
    def routes():
        from_query = request.args.get("from", "").strip()
        to_query = request.args.get("to", "").strip()
        if not from_query or not to_query:
            return jsonify(error="from and to are required"), 400
        with Session(engine) as session:
            start = find_stop(session, from_query)
            end = find_stop(session, to_query)
            if not start or not end:
                return jsonify({"from": stop_json(start, from_query), "to": stop_json(end, to_query), "routes": []})
            results = route_candidates(session, start, end)
            unique_results = {result["id"]: result for result in results}
            results = list(unique_results.values())
            results.sort(key=route_sort_key)
            results, ranking_source = rank_routes_with_ai(results, from_query, to_query)
            results.sort(key=lambda item: item["changes"])
            for index, result in enumerate(results):
                result["recommended"] = index == 0
                result["rankingSource"] = ranking_source
            return jsonify({"from": stop_json(start), "to": stop_json(end), "routes": results[:20]})

    @app.post("/api/chat")
    def chat():
        body = request.get_json(silent=True) or {}
        question = str(body.get("message", "")).strip()
        from_query = str(body.get("from", "")).strip()
        to_query = str(body.get("to", "")).strip()
        if not question:
            return jsonify(error="message is required"), 400

        with Session(engine) as session:
            # If the user did not fill the main search boxes, detect stop names
            # directly from the imported JSON-backed stop database.
            detected_from, detected_to = detect_route_places(session, question)
            route_question = bool(
                re.search(r"\b(bus|buses|route|routes|stop|from|to|via|go|goes|jana|jaye|jati|jaati|safar)\b", question.casefold())
                or detected_from
                or detected_to
            )
            from_query = (from_query if route_question else "") or detected_from or ""
            to_query = (to_query if route_question else "") or detected_to or ""

            candidates = []
            start = find_stop(session, from_query) if from_query else None
            end = find_stop(session, to_query) if to_query else None
            if start and end and start.id != end.id:
                candidates = route_candidates(session, start, end)

            # For questions such as "What buses go to Saddar?" or "Which
            # buses serve NIPA?", return database-grounded service information
            # even when a complete origin/destination pair is not supplied.
            if not candidates:
                candidates = service_routes_for_question(session, question, start, end)

            answer, answer_source = answer_with_groq(question, from_query, to_query, candidates, body.get("history", []))
            return jsonify(
                answer=answer,
                source=answer_source,
                fromStop=stop_json(start, from_query) if from_query else None,
                toStop=stop_json(end, to_query) if to_query else None,
                routes=candidates[:10],
            )

    @app.get("/media/<path:filename>")
    def media(filename):
        return send_from_directory(RAW_IMAGE_DIR, filename)

    @app.get("/assets/<path:filename>")
    def attached_assets(filename):
        return send_from_directory(ATTACHED_ASSET_DIR, filename)

    @app.get("/public/<path:filename>")
    def public_assets(filename):
        return send_from_directory(PROJECT_DIR / "public", filename)

    @app.get("/")
    def frontend():
        response = send_from_directory(PROJECT_DIR, "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def seed_database(engine):
    with Session(engine) as session:
        extracted_routes = load_extracted_routes()
        existing_codes = set(session.scalars(select(Bus.route_code)).all())
        extracted_codes = {route["code"] for route in extracted_routes}
        if existing_codes and extracted_codes.issubset(existing_codes):
            return
        if existing_codes and engine.url.get_backend_name() == "sqlite":
            session.execute(delete(RouteStop))
            session.execute(delete(Route))
            session.execute(delete(Bus))
            session.execute(delete(Stop))
            session.commit()
        seed_stops = {}
        for route in extracted_routes:
            for stop_name in route["stops"]:
                seed_stops.setdefault(stop_name, make_extracted_stop(stop_name))
        used_stop_ids = set()
        for stop in seed_stops.values():
            base_id = stop["id"]
            stop_id = base_id
            suffix = 2
            while stop_id in used_stop_ids:
                stop_id = f"{base_id}-{suffix}"
                suffix += 1
            stop["id"] = stop_id
            used_stop_ids.add(stop_id)
        session.add_all(Stop(**item) for item in seed_stops.values())
        buses = []
        attached_images = sorted(path.name for path in ATTACHED_ASSET_DIR.glob("*.jpeg"))
        for route_index, route in enumerate(extracted_routes):
            source_image = route.get("source_image")
            if not source_image or not (RAW_IMAGE_DIR / source_image).is_file():
                raise ValueError(f"Route {route.get('code')} has no valid source image")
            bus = {
                "id": f"bus-{slugify(route['code'])}",
                "route_code": route["code"],
                "name": route["name"],
                "vehicle_type": route["vehicle_type"],
                "has_ac": route["vehicle_type"] == "ev",
                "has_wheelchair": route["vehicle_type"] == "ev",
                "category": route["category"],
                "image_filename": attached_images[route_index % len(attached_images)] if attached_images else None,
                "source_image": source_image,
            }
            buses.append(Bus(**bus))
        session.add_all(buses)
        session.flush()
        stops_by_name = {stop.name: stop for stop in session.scalars(select(Stop)).all()}
        for extracted, bus in zip(extracted_routes, buses):
            route = Route(
                id=f"route-{slugify(extracted['code'])}",
                bus_id=bus.id,
                duration_minutes=0,
                frequency_minutes=None,
                fare_label="Ask conductor",
                fare_kind="variable",
            )
            session.add(route)
            session.flush()
            seen_stop_ids = set()
            route_links = []
            for name in extracted["stops"]:
                stop_id = stops_by_name[name].id
                if stop_id in seen_stop_ids:
                    continue
                seen_stop_ids.add(stop_id)
                route_links.append(RouteStop(route_id=route.id, stop_id=stop_id, stop_order=len(route_links) + 1))
            session.add_all(route_links)
        session.commit()


def upgrade_schema(engine):
    inspector = inspect(engine)
    if "buses" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("buses")}
    column_types = {"category": "VARCHAR(40)", "source_image": "VARCHAR(255)"}
    missing_columns = set(column_types) - columns
    if not missing_columns:
        return
    with engine.begin() as connection:
        if engine.url.get_backend_name() == "sqlite":
            connection.execute(text("DROP TABLE IF EXISTS route_stops"))
            connection.execute(text("DROP TABLE IF EXISTS routes"))
            connection.execute(text("DROP TABLE IF EXISTS buses"))
            connection.execute(text("DROP TABLE IF EXISTS stops"))
        else:
            for column in missing_columns:
                connection.execute(text(f"ALTER TABLE buses ADD COLUMN {column} {column_types[column]}"))


def load_extracted_routes() -> list[dict]:
    data_file = BACKEND_DIR / "data" / "extracted_routes.json"
    with data_file.open(encoding="utf-8") as file:
        return json.load(file)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def make_extracted_stop(name: str) -> dict:
    aliases = {
        "Saddar": ["sadar"],
        "Liaquatabad 10": ["liaqatabad 10"],
        "Nagan Chorangi": ["Nagan"],
        "NIPA Chowrangi": ["nipa"],
        "NIPA Chorangi": ["nipa chorangi"],
    }.get(name, [])
    return {"id": f"stop-{slugify(name)}", "name": name, "name_ur": None, "aliases": aliases, "latitude": 24.86, "longitude": 67.01}


def find_stop(session: Session, value: str) -> Stop | None:
    value = value.lower()
    stops = session.scalars(select(Stop).order_by(Stop.name)).all()
    canonical_names = {"sadar": "saddar", "nagan": "nagan chorangi", "nipa": "nipa chowrangi"}
    value = canonical_names.get(value, value)
    for stop in stops:
        if value in [alias.lower() for alias in (stop.aliases or [])]:
            return stop
    for stop in stops:
        if value == stop.name.lower():
            return stop
    for stop in stops:
        haystack = " ".join([stop.name, stop.name_ur or "", *(stop.aliases or [])]).lower()
        if value in haystack:
            return stop
    return None



def detect_route_places(session: Session, question: str) -> tuple[str | None, str | None]:
    """Find up to two known Karachi stops mentioned in a natural-language question."""
    q = question.casefold()
    stops = session.scalars(select(Stop).order_by(Stop.name)).all()
    matches = []
    for stop in stops:
        names = [stop.name, *(stop.aliases or [])]
        best = None
        for name in names:
            name = str(name).strip()
            if len(name) < 3:
                continue
            pos = q.find(name.casefold())
            if pos >= 0:
                # Prefer the longest matching name when aliases overlap.
                score = (len(name), -pos)
                if best is None or score > best[0]:
                    best = (score, pos)
        if best:
            match_length = best[0][0]
            matches.append((best[1], -match_length, stop.name, best[1] + match_length))
    matches.sort()
    names = []
    occupied = []
    for start, _, name, end in matches:
        if any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in occupied):
            continue
        if name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
            occupied.append((start, end))
        if len(names) == 2:
            break

    if len(names) < 2:
        return None, names[0] if names else None

    # Natural-language direction: "from A to B", "A to B", "A se B".
    lowered = q
    first, second = names[0], names[1]
    between_to = re.search(
        rf"\\b(?:from|between|via|near)\\b.*?{re.escape(first.casefold())}.*?\\b(?:to|and|se|say|tak)\\b.*?{re.escape(second.casefold())}",
        lowered,
    )
    if between_to or re.search(r"\\b(?:to|tak|se)\\b", lowered):
        return first, second
    return first, second


def service_routes_for_question(
    session: Session,
    question: str,
    start: Stop | None,
    end: Stop | None,
) -> list[dict]:
    """Return routes grounded in the JSON import for stop/service questions."""
    all_routes = session.scalars(select(Route).where(Route.active.is_(True))).all()
    q = question.casefold()
    bus_matches = [
        route for route in all_routes
        if re.search(rf"(?<!\w){re.escape(route.bus.route_code.casefold())}(?!\w)", q)
        or route.bus.name.casefold() in q
    ]
    target = end or start
    candidate_routes = bus_matches or all_routes if target else bus_matches
    if not candidate_routes:
        return []

    target_results = []
    for route in candidate_routes:
        ordered_stops = [item.stop for item in route.route_stops]
        if target and not any(stop.id == target.id for stop in ordered_stops):
            continue
        # If the question asks about AC/electric/wheelchair, keep relevant buses first.
        feature_match = (
            ("ac" in q or "air condition" in q) and route.bus.has_ac
        ) or (
            ("electric" in q or "ev bus" in q) and route.bus.vehicle_type == "ev"
        ) or (
            ("wheelchair" in q) and route.bus.has_wheelchair
        )
        item = route_json(route, ordered_stops)
        item["relevantFeature"] = bool(feature_match)
        target_results.append(item)

    target_results.sort(key=lambda item: (not item.get("relevantFeature", False), item["routeCode"]))
    return target_results[:10]


def route_candidates(session: Session, start: Stop, end: Stop) -> list[dict]:
    results = []
    available_routes = session.scalars(select(Route).where(Route.active.is_(True))).all()
    for route in available_routes:
        ordered_stops = [item.stop for item in route.route_stops]
        start_index = index_of(ordered_stops, start.id)
        end_index = index_of(ordered_stops, end.id)
        if start_index >= 0 and end_index >= 0 and start_index != end_index:
            segment = ordered_stops[start_index:end_index + 1] if start_index < end_index else list(reversed(ordered_stops[end_index:start_index + 1]))
            results.append(route_json(route, segment))
    results.extend(transfer_recommendations(available_routes, start, end))
    unique_results = {result["id"]: result for result in results}
    results = list(unique_results.values())
    results.sort(key=route_sort_key)
    return results[:20]


def index_of(stops: list[Stop], stop_id: str) -> int:
    return next((index for index, stop in enumerate(stops) if stop.id == stop_id), -1)


def stop_json(stop: Stop | None, fallback: str | None = None) -> dict | None:
    if not stop:
        return {"name": fallback} if fallback else None
    return {"id": stop.id, "name": stop.name, "nameUrdu": stop.name_ur, "latitude": float(stop.latitude), "longitude": float(stop.longitude)}


def route_json(route: Route, stops: list[Stop]) -> dict:
    distance, coordinates, distance_source = route_map_data(stops)
    return {
        "id": route.id,
        "routeCode": route.bus.route_code,
        "busCodes": [route.bus.route_code],
        "busName": route.bus.name,
        "vehicleType": route.bus.vehicle_type,
        "durationMinutes": None,
        "distanceKm": distance,
        "distanceSource": distance_source,
        "frequencyMinutes": route.frequency_minutes,
        "changes": 0,
        "fare": {"label": route.fare_label, "kind": route.fare_kind},
        "features": [feature for feature, enabled in (("ac", route.bus.has_ac), ("wheelchair", route.bus.has_wheelchair)) if enabled],
        "category": route.bus.category,
        "stops": [stop.name for stop in stops],
        "coordinates": coordinates,
        "geometry": route.geometry,
        "imageUrl": f"/assets/{route.bus.image_filename}" if route.bus.image_filename else None,
        "sourceImage": route.bus.source_image,
        "legs": [],
    }


def transfer_recommendations(routes: list[Route], start: Stop, end: Stop) -> list[dict]:
    recommendations = []
    for first in routes:
        first_stops = [item.stop for item in first.route_stops]
        start_index = index_of(first_stops, start.id)
        if start_index < 0:
            continue
        for second in routes:
            if first.id == second.id:
                continue
            second_stops = [item.stop for item in second.route_stops]
            end_index = index_of(second_stops, end.id)
            if end_index < 0:
                continue
            shared_stops = {stop.id for stop in first_stops[start_index + 1:]}
            transfer_index = next((index for index, stop in enumerate(second_stops[:end_index]) if stop.id in shared_stops), -1)
            if transfer_index < 0:
                continue
            transfer_stop = second_stops[transfer_index]
            first_segment = first_stops[start_index: index_of(first_stops, transfer_stop.id) + 1]
            second_segment = second_stops[transfer_index:end_index + 1]
            frequencies = [value for value in (first.frequency_minutes, second.frequency_minutes) if value]
            combined_stops = first_segment + second_segment[1:]
            distance, coordinates, distance_source = route_map_data(combined_stops)
            recommendations.append({
                "id": f"transfer-{first.id}-{second.id}",
                "routeCode": f"{first.bus.route_code} then {second.bus.route_code}",
                "busCodes": [first.bus.route_code, second.bus.route_code],
                "busName": f"{first.bus.name} to {transfer_stop.name}, then {second.bus.name}",
                "vehicleType": first.bus.vehicle_type,
                "durationMinutes": None,
                "distanceKm": distance,
                "distanceSource": distance_source,
                "frequencyMinutes": min(frequencies) if frequencies else None,
                "changes": 1,
                "fare": {"label": "Ask conductor", "kind": "variable"},
                "features": [],
                "category": "transfer",
                "transferStop": transfer_stop.name,
                "stops": [stop.name for stop in combined_stops],
                "coordinates": coordinates,
                "geometry": None,
                "imageUrl": f"/assets/{first.bus.image_filename}" if first.bus.image_filename else None,
                "sourceImage": first.bus.source_image,
                "sourceImages": [first.bus.source_image, second.bus.source_image],
                "legs": [
                    {"routeCode": first.bus.route_code, "stops": [stop.name for stop in first_segment]},
                    {"routeCode": second.bus.route_code, "stops": [stop.name for stop in second_segment]},
                ],
            })
    return recommendations[:10]


def route_distance(stops: list[Stop]) -> float | None:
    if len({(float(stop.latitude), float(stop.longitude)) for stop in stops}) < 2:
        return None
    total = 0.0
    for current, following in zip(stops, stops[1:]):
        latitude_one, longitude_one = float(current.latitude), float(current.longitude)
        latitude_two, longitude_two = float(following.latitude), float(following.longitude)
        delta_lat = math.radians(latitude_two - latitude_one)
        delta_lng = math.radians(longitude_two - longitude_one)
        average_latitude = math.radians((latitude_one + latitude_two) / 2)
        total += 6371 * math.sqrt(delta_lat ** 2 + (math.cos(average_latitude) * delta_lng) ** 2)
    return round(total, 1)


def route_map_data(stops: list[Stop]) -> tuple[float | None, list[list[float]], str]:
    local_coordinates = [[float(stop.latitude), float(stop.longitude)] for stop in stops]
    if len({tuple(coordinate) for coordinate in local_coordinates}) > 1:
        return route_distance(stops), local_coordinates, "database"
    if os.getenv("GEOCODING_ENABLED", "1") != "1" or len(stops) < 2:
        return None, local_coordinates, "unavailable"
    midpoint = stops[len(stops) // 2]
    waypoint_stops = [stops[0], midpoint, stops[-1]]
    waypoints = []
    for stop in waypoint_stops:
        coordinate = geocode_stop(stop.name)
        if coordinate and coordinate not in waypoints:
            waypoints.append(coordinate)
    if len(waypoints) < 2:
        return None, local_coordinates, "unavailable"
    route = road_route_waypoints(waypoints)
    if not route:
        return None, [[latitude, longitude] for longitude, latitude in waypoints], "geocoded"
    return route["distanceKm"], route["coordinates"], "osrm"


def geocode_stop(name: str) -> tuple[float, float] | None:
    key = name.lower().strip()
    if key in GEOCODE_CACHE:
        return GEOCODE_CACHE[key]
    query = urllib.parse.quote(f"{name}, Karachi, Pakistan")
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q={query}",
        headers={"User-Agent": "Safarz Karachi Bus Finder/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            results = json.loads(response.read().decode("utf-8"))
        if not results:
            GEOCODE_CACHE[key] = None
            return None
        coordinates = (float(results[0]["lon"]), float(results[0]["lat"]))
        GEOCODE_CACHE[key] = coordinates
        return coordinates
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def road_route(origin: tuple[float, float], destination: tuple[float, float]) -> dict | None:
    return road_route_waypoints([origin, destination])


def road_route_waypoints(waypoints: list[tuple[float, float]]) -> dict | None:
    key = tuple(value for waypoint in waypoints for value in waypoint)
    if key in ROUTE_CACHE:
        return ROUTE_CACHE[key]
    locations = ";".join(f"{longitude},{latitude}" for longitude, latitude in waypoints)
    request = urllib.request.Request(
        f"https://router.project-osrm.org/route/v1/driving/{locations}?overview=full&geometries=geojson",
        headers={"User-Agent": "Safarz Karachi Bus Finder/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            body = json.loads(response.read().decode("utf-8"))
        route = body["routes"][0]
        result = {"distanceKm": round(route["distance"] / 1000, 1), "coordinates": [[lat, lon] for lon, lat in route["geometry"]["coordinates"]]}
        ROUTE_CACHE[key] = result
        return result
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def route_sort_key(route: dict) -> tuple:
    return (route["changes"], route["distanceKm"] is None, route["distanceKm"] or float("inf"), len(route["stops"]))


def rank_routes_with_ai(routes: list[dict], from_query: str, to_query: str) -> tuple[list[dict], str]:
    """Let an optional model choose among database routes, never create routes."""
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key or os.getenv("GROQ_RANKING", "1") != "1" or len(routes) < 2:
        return routes, "database"
    endpoint = os.getenv("GROQ_API_URL", os.getenv("AI_API_URL", "https://api.groq.com/openai/v1/chat/completions"))
    payload = {
        "model": os.getenv("GROQ_MODEL", os.getenv("AI_MODEL", "openai/gpt-oss-20b")),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Rank only the supplied route IDs. Never invent, remove, or modify a route. Prefer direct routes, then fewer changes, then shorter distance. Return JSON: {\"route_ids\": [\"id\"]}."},
            {"role": "user", "content": json.dumps({"from": from_query, "to": to_query, "routes": [{"id": route["id"], "routeCode": route["routeCode"], "changes": route["changes"], "distanceKm": route["distanceKm"], "stops": route["stops"], "legs": route["legs"]} for route in routes]}, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Safarz Karachi Bus Finder/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        ordered_ids = json.loads(content).get("route_ids", [])
        allowed = {route["id"]: route for route in routes}
        if set(ordered_ids) != set(allowed) or len(ordered_ids) != len(routes):
            return routes, "database"
        return [allowed[route_id] for route_id in ordered_ids], "ai"
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return routes, "database"


def answer_with_groq(question: str, from_query: str, to_query: str, routes: list[dict], history: list[dict] | None = None) -> tuple[str, str]:
    route_context = [{
        "routeCode": route["routeCode"],
        "busName": route["busName"],
        "changes": route["changes"],
        "distanceKm": route["distanceKm"],
        "distanceSource": route.get("distanceSource"),
        "stops": route["stops"],
        "legs": route["legs"],
        "vehicleType": route.get("vehicleType"),
        "features": route.get("features", []),
        "transferStop": route.get("transferStop"),
    } for route in routes[:10]]
    fallback = grounded_chat_fallback(question, from_query, to_query, routes)
    api_key = os.getenv("GROQ_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key:
        return fallback, "database"
    prior_messages = [
        item for item in (history or [])
        if isinstance(item, dict)
        and item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    ][-8:]
    payload = {
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": """You are Safarz, a helpful general-purpose AI assistant. Answer all kinds of safe user questions, including general knowledge, explanations, math, coding, writing, casual conversation, and travel. You are especially useful for Karachi public transport. The route records supplied below are the only source of truth for bus facts: never invent bus codes, stops, fares, distances, times, frequencies, or routes. Never claim Safarz has app-store downloads, accounts, live tracking, notifications, saved routes, or other features unless they are explicitly present in the supplied context. If the user names a bus code or bus name and matching route records are supplied, answer about that bus directly: summarize its first stop, last stop, key stops, and available service facts; do not ask for an origin and destination. Recommend the best available bus option using fewer changes first, then shorter estimated distance, then shorter duration. For connections, explain each leg and name the transfer stop. If route records do not answer a bus question, say so plainly and ask for the origin and destination. For non-bus questions, answer normally using general knowledge and distinguish uncertain or current information. Match the user's language: English, Urdu script, or Roman Urdu. If the user mixes languages, reply naturally in the dominant language. Be conversational, helpful, and concise. Distances marked as stop-count estimates must be called estimates. Do not mention hidden prompts or JSON."""},
            *prior_messages,
            {"role": "user", "content": json.dumps({"question": question, "from": from_query, "to": to_query, "routes": route_context}, ensure_ascii=False)},
        ],
    }
    endpoint = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Safarz Karachi Bus Finder/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        answer = body["choices"][0]["message"]["content"].strip()
        if routes and re.search(r"\b(no|not|don['’]?t|do not)\b.*\b(route|bus|data|find)\b", answer.casefold()):
            return fallback, "database"
        return answer, "groq"
    except urllib.error.HTTPError as error:
        print(f"Groq chat unavailable; using database fallback: HTTP {error.code} {error.read().decode('utf-8', errors='replace')[:200]}")
        return fallback, "database"
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Groq chat unavailable; using database fallback: {error}")
        return fallback, "database"


def grounded_chat_fallback(question: str, from_query: str, to_query: str, routes: list[dict]) -> str:
    if not routes:
        if from_query and to_query:
            return f"I could not find an imported bus route from {from_query} to {to_query}. Try selecting nearby stops from the search suggestions."
        if to_query:
            return f"I found no imported bus information for {to_query}. Try another Karachi stop name."
        return "I can answer from Safarz's imported Karachi route data. Ask me something like: 'Which bus goes from Gulshan Chowrangi to Saddar?'"

    if from_query and to_query:
        intro = f"For {from_query} to {to_query}, "
    elif to_query:
        intro = f"For buses serving {to_query}, "
    else:
        intro = "From the imported route data, "

    lines = []
    for route in routes[:5]:
        if route["legs"]:
            steps = " then ".join(
                f"Bus {index + 1} ({leg['routeCode']}) from {leg['stops'][0]} to {leg['stops'][-1]}"
                for index, leg in enumerate(route["legs"])
            )
        else:
            steps = f"Bus {route['routeCode']} from {route['stops'][0]} to {route['stops'][-1]}"
        distance = (
            "distance unavailable"
            if route["distanceKm"] is None
            else f"{route['distanceKm']} km"
        )
        change_text = "direct" if route["changes"] == 0 else f"{route['changes']} change"
        lines.append(f"{steps} — {distance}, {change_text}")
    return intro + "; ".join(lines) + "."


if os.getenv("RESET_DATABASE") == "1" and DEFAULT_DATABASE_URL.startswith("sqlite"):
    database_file = Path(DEFAULT_DATABASE_URL.removeprefix("sqlite:///"))
    if database_file.exists():
        database_file.unlink()

app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")