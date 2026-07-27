import os
import json
import threading
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

class DatabaseManager:
    """
    Менеджер единственного постоянного подключения к PostgreSQL 
    с проверкой жизнеспособности (liveness check) и сессией read-only.
    """
    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def get_connection(self):
        with self._lock:
            # Проверяем, открыто ли соединение и отвечает ли сервер
            if self._conn is not None and not self._conn.closed:
                try:
                    with self._conn.cursor() as cur:
                        cur.execute("SELECT 1;")
                    return self._conn
                except Exception:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None

            # Создаем новое постоянное подключение
            self._conn = psycopg2.connect(
                dbname=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                host=os.getenv("DB_HOST"),
                port=os.getenv("DB_PORT")
            )
            # Устанавливаем режим только для чтения и автокоммит
            self._conn.set_session(readonly=True, autocommit=True)
            return self._conn

db_manager = DatabaseManager()

def get_db_connection():
    """Возвращает единое постоянное подключение к БД."""
    return db_manager.get_connection()

def get_nearest_stops(lon, lat, radius_meters=1000, limit=2):
    """
    Находит ближайшие остановки к указанным координатам в заданном радиусе.
    Использует PostGIS оператор <-> для KNN-поиска и приведение к geography для метров.
    """
    query = """
    SELECT 
        id, 
        title, 
        zone, 
        ST_X(geom) as lon, 
        ST_Y(geom) as lat,
        ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) as distance_m
    FROM stops
    WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
    ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
    LIMIT %s;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (lon, lat, lon, lat, radius_meters, lon, lat, limit))
        return cur.fetchall()

def get_stops_nearby(lon, lat, radius_meters=400):
    """
    Возвращает список уникальных остановок рядом с точкой, включая количество маршрутов.
    """
    query = """
    SELECT 
        s.id, 
        s.title, 
        ST_X(s.geom) as lon, 
        ST_Y(s.geom) as lat,
        ST_Distance(s.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) as distance_m,
        COUNT(DISTINCT rs.route_id) as routes_count
    FROM stops s
    LEFT JOIN route_stops rs ON s.id = rs.stop_id
    WHERE ST_DWithin(s.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
    GROUP BY s.id
    ORDER BY distance_m;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (lon, lat, lon, lat, radius_meters))
        results = []
        for r in cur.fetchall():
            r['title'] = r['title'].strip()
            results.append(r)
        return results

def get_buses_at_stop(stop_id):
    """
    Возвращает список номеров автобусов, проходящих через остановку.
    """
    query = """
    SELECT DISTINCT r.num
    FROM route_stops rs
    JOIN routes r ON rs.route_id = r.id
    WHERE rs.stop_id = %s
    ORDER BY r.num;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (stop_id,))
        return [r['num'] for r in cur.fetchall()]

def get_stop_title(stop_id):
    """Возвращает название остановки по её ID."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT title FROM stops WHERE id = %s", (stop_id,))
        res = cur.fetchone()
        return res[0].strip() if res else "Неизвестная остановка"

def get_buses_with_destinations(stop_id):
    """
    Возвращает список автобусов с их конечными остановками 
    с учетом направления движения от текущей остановки.
    """
    query = """
    SELECT DISTINCT 
        r.num, 
        r.title as route_title,
        s_end.title as destination,
        rs_current.stop_sequence as current_seq,
        (SELECT MAX(stop_sequence) FROM route_stops WHERE route_id = r.id) as max_seq
    FROM route_stops rs_current
    JOIN routes r ON rs_current.route_id = r.id
    JOIN route_stops rs_end ON r.id = rs_end.route_id
    JOIN stops s_end ON rs_end.stop_id = s_end.id
    WHERE rs_current.stop_id = %s
      AND rs_end.stop_sequence = (
          SELECT MAX(stop_sequence) 
          FROM route_stops 
          WHERE route_id = r.id
      )
    ORDER BY r.num;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (stop_id,))
        results = cur.fetchall()
        
        processed = []
        for res in results:
            if res['current_seq'] == res['max_seq']:
                dest = "Конечная"
            else:
                dest = res['destination'].strip()
            
            processed.append({
                "num": res['num'],
                "destination": dest,
                "route_title": res['route_title'].strip()
            })
        return processed

def search_routes_by_prefix(prefix):
    """Находит все маршруты, номера которых начинаются с указанного префикса и имеют геометрию."""
    query = """
    SELECT num, title
    FROM routes
    WHERE num ILIKE %s AND geom IS NOT NULL
    ORDER BY num;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (f"{prefix}%",))
        results = []
        for r in cur.fetchall():
            r['title'] = r['title'].strip()
            results.append(r)
        return results

def get_direct_routes(start_stop_id, end_stop_id):
    """
    Находит прямые маршруты (без пересадок) между двумя остановками.
    Проверяет, что начальная остановка идет раньше конечной в последовательности маршрута.
    """
    query = """
    SELECT 
        r.id as route_id,
        r.num,
        r.title as route_title,
        rs1.stop_sequence as start_seq,
        rs2.stop_sequence as end_seq
    FROM route_stops rs1
    JOIN route_stops rs2 ON rs1.route_id = rs2.route_id
    JOIN routes r ON rs1.route_id = r.id
    WHERE rs1.stop_id = %s 
      AND rs2.stop_id = %s
      AND rs1.stop_sequence < rs2.stop_sequence
    ORDER BY r.num;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (start_stop_id, end_stop_id))
        results = []
        for r in cur.fetchall():
            r['route_title'] = r['route_title'].strip()
            results.append(r)
        return results

def get_transfer_routes(start_stop_id, end_stop_id):
    """
    Находит варианты проезда с одной пересадкой.
    Логика: R1 (start -> transfer) + R2 (transfer -> end)
    """
    query = """
    SELECT 
        r1.num as bus1, 
        r1.title as route1_title,
        s_trans.id as transfer_stop_id,
        s_trans.title as transfer_stop_title,
        r2.num as bus2,
        r2.title as route2_title
    FROM route_stops rs1_start
    JOIN route_stops rs1_trans ON rs1_start.route_id = rs1_trans.route_id
    JOIN route_stops rs2_trans ON rs1_trans.stop_id = rs2_trans.stop_id
    JOIN route_stops rs2_end ON rs2_trans.route_id = rs2_end.route_id
    JOIN routes r1 ON rs1_start.route_id = r1.id
    JOIN routes r2 ON rs2_end.route_id = r2.id
    JOIN stops s_trans ON rs1_trans.stop_id = s_trans.id
    WHERE rs1_start.stop_id = %s
      AND rs2_end.stop_id = %s
      AND rs1_start.stop_sequence < rs1_trans.stop_sequence
      AND rs2_trans.stop_sequence < rs2_end.stop_sequence
      AND rs1_start.route_id != rs2_end.route_id
    ORDER BY r1.num, r2.num
    LIMIT 10;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (start_stop_id, end_stop_id))
        results = []
        for r in cur.fetchall():
            r['route1_title'] = r['route1_title'].strip()
            r['transfer_stop_title'] = r['transfer_stop_title'].strip()
            r['route2_title'] = r['route2_title'].strip()
            results.append(r)
        return results

def get_route_segment_geom(route_id, start_stop_id, end_stop_id):
    """
    Возвращает геометрию участка маршрута между двумя остановками.
    Строит путь последовательно через все промежуточные остановки,
    чтобы гарантировать правильное отображение даже на сложных маршрутах.
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        query_stops = """
        SELECT rs.stop_id, rs.stop_sequence
        FROM route_stops rs
        WHERE rs.route_id = %s 
          AND rs.stop_sequence >= (SELECT stop_sequence FROM route_stops WHERE route_id = %s AND stop_id = %s LIMIT 1)
          AND rs.stop_sequence <= (SELECT stop_sequence FROM route_stops WHERE route_id = %s AND stop_id = %s LIMIT 1)
        ORDER BY rs.stop_sequence;
        """
        cur.execute(query_stops, (route_id, route_id, start_stop_id, route_id, end_stop_id))
        segment_stops = cur.fetchall()
        
        if len(segment_stops) < 2:
            return None
        
        segments_wkt = []
        for i in range(len(segment_stops) - 1):
            s1_id = segment_stops[i]['stop_id']
            s2_id = segment_stops[i+1]['stop_id']
            
            query_seg = """
            SELECT ST_AsText(
                CASE 
                    WHEN ST_LineLocatePoint(r.geom, s1.geom) < ST_LineLocatePoint(r.geom, s2.geom)
                    THEN ST_LineSubstring(r.geom, ST_LineLocatePoint(r.geom, s1.geom), ST_LineLocatePoint(r.geom, s2.geom))
                    ELSE ST_LineSubstring(r.geom, ST_LineLocatePoint(r.geom, s2.geom), ST_LineLocatePoint(r.geom, s1.geom))
                END
            ) as wkt
            FROM routes r, stops s1, stops s2
            WHERE r.id = %s AND s1.id = %s AND s2.id = %s
              AND r.geom IS NOT NULL;
            """
            cur.execute(query_seg, (route_id, s1_id, s2_id))
            res = cur.fetchone()
            if res and res['wkt']:
                segments_wkt.append(res['wkt'])
        
        if not segments_wkt:
            return None
            
        cur.execute("SELECT ST_AsGeoJSON(ST_LineMerge(ST_Collect(ST_GeomFromText(s)))) as geojson FROM unnest(%s) s", (segments_wkt,))
        result = cur.fetchone()
        return result['geojson'] if result else None

def get_intermediate_stops_geom(route_id, start_seq, end_seq):
    """
    Возвращает список геометрий всех остановок на маршруте между start_seq и end_seq.
    """
    query = """
    SELECT ST_AsGeoJSON(s.geom) as geojson
    FROM route_stops rs
    JOIN stops s ON rs.stop_id = s.id
    WHERE rs.route_id = %s 
      AND rs.stop_sequence >= %s 
      AND rs.stop_sequence <= %s
    ORDER BY rs.stop_sequence;
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (route_id, start_seq, end_seq))
        return [json.loads(r['geojson']) for r in cur.fetchall()]

def get_route_full_details(route_num, exact=False):
    """
    Возвращает полную информацию о маршруте по его номеру:
    геометрию, список остановок и пометки пересадочных узлов (>7 маршрутов).
    """
    conn = get_db_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if exact:
            cur.execute("SELECT id, num, title, ST_AsGeoJSON(geom) as geojson FROM routes WHERE num = %s AND geom IS NOT NULL LIMIT 1", (route_num.upper(),))
        else:
            cur.execute("SELECT id, num, title, ST_AsGeoJSON(geom) as geojson FROM routes WHERE num ILIKE %s AND geom IS NOT NULL LIMIT 1", (route_num,))
        
        route = cur.fetchone()
        if not route:
            return None

        query = """
        SELECT 
            s.id, s.title, ST_X(s.geom) as lon, ST_Y(s.geom) as lat,
            rs.stop_sequence,
            (SELECT COUNT(DISTINCT rs2.route_id) FROM route_stops rs2 WHERE rs2.stop_id = s.id) as routes_at_stop
        FROM route_stops rs
        JOIN stops s ON rs.stop_id = s.id
        WHERE rs.route_id = %s
        ORDER BY rs.stop_sequence;
        """
        cur.execute(query, (route['id'],))
        stops = []
        for r in cur.fetchall():
            r['title'] = r['title'].strip()
            stops.append(r)
        
        return {
            "route_id": route['id'],
            "num": route['num'],
            "title": route['title'].strip(),
            "geojson": route['geojson'],
            "stops": stops
        }

def find_optimal_route(start_lon, start_lat, end_lon, end_lat):
    """
    Находит оптимальные маршруты между двумя точками.
    Ищет все остановки в радиусе 500м от старта и финиша.
    """
    user_coords = {"start": (start_lat, start_lon), "end": (end_lat, end_lon)}
    
    start_stops = get_nearest_stops(start_lon, start_lat, radius_meters=500, limit=10)
    end_stops = get_nearest_stops(end_lon, end_lat, radius_meters=500, limit=10)
    
    if not start_stops or not end_stops:
        return []

    found_routes = []
    seen_routes = set()

    conn = get_db_connection()

    for s_start in start_stops:
        for s_end in end_stops:
            direct_query = """
            SELECT r.id as route_id, r.num, r.title as route_title, 
                   rs1.stop_sequence as start_seq, rs2.stop_sequence as end_seq
            FROM route_stops rs1
            JOIN route_stops rs2 ON rs1.route_id = rs2.route_id
            JOIN routes r ON rs1.route_id = r.id
            WHERE rs1.stop_id = %s AND rs2.stop_id = %s AND rs1.stop_sequence < rs2.stop_sequence
            """
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(direct_query, (s_start['id'], s_end['id']))
                direct = cur.fetchall()
                for r in direct:
                    key = (r['num'], 'direct')
                    if key not in seen_routes:
                        found_routes.append({
                            "type": "direct",
                            "route": r['num'],
                            "title": r['route_title'],
                            "stops_count": r['end_seq'] - r['start_seq'],
                            "start_stop": s_start['title'],
                            "end_stop": s_end['title'],
                            "dist_to_start": s_start['distance_m'],
                            "user_coords": user_coords,
                            "mapping_data": [{
                                "route_id": r['route_id'],
                                "bus_num": r['num'],
                                "start_id": s_start['id'],
                                "end_id": s_end['id'],
                                "start_seq": r['start_seq'],
                                "end_seq": r['end_seq']
                            }]
                        })
                        seen_routes.add(key)
            
            if len([x for x in found_routes if x['type'] == 'direct']) < 2:
                transfer_query = """
                SELECT 
                    r1.id as r1_id, r1.num as bus1, r1.title as title1,
                    s1_trans.id as trans1_id, s1_trans.title as trans1_title,
                    s2_trans.id as trans2_id, s2_trans.title as trans2_title,
                    r2.id as r2_id, r2.num as bus2, r2.title as title2,
                    rs1_start.stop_sequence as s1, rs1_trans.stop_sequence as t1,
                    rs2_trans.stop_sequence as t2, rs2_end.stop_sequence as e2,
                    ST_Distance(s1_trans.geom::geography, s2_trans.geom::geography) as walk_dist
                FROM route_stops rs1_start
                JOIN route_stops rs1_trans ON rs1_start.route_id = rs1_trans.route_id
                JOIN stops s1_trans ON rs1_trans.stop_id = s1_trans.id
                JOIN stops s2_trans ON ST_DWithin(s1_trans.geom::geography, s2_trans.geom::geography, 500)
                JOIN route_stops rs2_trans ON s2_trans.id = rs2_trans.stop_id
                JOIN route_stops rs2_end ON rs2_trans.route_id = rs2_end.route_id
                JOIN routes r1 ON rs1_start.route_id = r1.id
                JOIN routes r2 ON rs2_end.route_id = r2.id
                WHERE rs1_start.stop_id = %s AND rs2_end.stop_id = %s
                  AND rs1_start.stop_sequence < rs1_trans.stop_sequence
                  AND rs2_trans.stop_sequence < rs2_end.stop_sequence
                  AND rs1_start.route_id != rs2_end.route_id
                ORDER BY (rs1_trans.stop_sequence - rs1_start.stop_sequence) + (rs2_end.stop_sequence - rs2_trans.stop_sequence) ASC
                LIMIT 15;
                """
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(transfer_query, (s_start['id'], s_end['id']))
                    transfer = cur.fetchall()
                    for r in transfer:
                        key = (f"{r['bus1']}->{r['bus2']}", 'transfer')
                        if key not in seen_routes:
                            cur.execute("SELECT ST_X(geom) as lon, ST_Y(geom) as lat FROM stops WHERE id = %s", (r['trans1_id'],))
                            t1_res = cur.fetchone()
                            cur.execute("SELECT ST_X(geom) as lon, ST_Y(geom) as lat FROM stops WHERE id = %s", (r['trans2_id'],))
                            t2_res = cur.fetchone()
                            
                            found_routes.append({
                                "type": "transfer",
                                "route1": r['bus1'], "route2": r['bus2'],
                                "stops_count": (r['t1'] - r['s1']) + (r['e2'] - r['t2']),
                                "walk_dist": r['walk_dist'],
                                "transfer_stop": r['trans1_title'],
                                "transfer2_stop": r['trans2_title'],
                                "start_stop": s_start['title'],
                                "end_stop": s_end['title'],
                                "dist_to_start": s_start['distance_m'],
                                "user_coords": user_coords,
                                "transfer_coords": (t1_res['lat'], t1_res['lon']),
                                "transfer2_coords": (t2_res['lat'], t2_res['lon']),
                                "mapping_data": [
                                    {"route_id": r['r1_id'], "bus_num": r['bus1'], "start_id": s_start['id'], "end_id": r['trans1_id'], "start_seq": r['s1'], "end_seq": r['t1']},
                                    {"route_id": r['r2_id'], "bus_num": r['bus2'], "start_id": r['trans2_id'], "end_id": s_end['id'], "start_seq": r['t2'], "end_seq": r['e2']}
                                ]
                            })
                            seen_routes.add(key)

    def sort_key(route):
        type_weight = 0 if route['type'] == 'direct' else 1
        if route['type'] == 'direct':
            name_score = len(route['route'])
        else:
            name_score = len(route['route1']) + len(route['route2'])
        dist_weight = route['dist_to_start'] // 50
        return (type_weight, name_score, dist_weight)

    found_routes.sort(key=sort_key)
    return found_routes[:3]
