import io
import json
import logging
import os
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import geopandas as gpd
import contextily as cx
from shapely.geometry import shape, Point, LineString
from dotenv import load_dotenv
from routing import get_route_segment_geom, get_intermediate_stops_geom

load_dotenv()

# Настройка matplotlib для работы без GUI
plt.switch_backend('Agg')

# Палитра цветов для маршрутов
ROUTE_COLORS = ['#1f77b4', '#2ca02c', '#9467bd', '#8c564b', '#e377c2', '#17becf']

logger = logging.getLogger(__name__)

# Настройка CARTO basemap с API ключом
CARTO_API_KEY = os.getenv('CARTO_API_KEY')
CARTO_ATTRIBUTION = '© OpenStreetMap contributors © CARTO'

if CARTO_API_KEY:
    CARTO_POSITRON_URL = f'https://basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png?key={CARTO_API_KEY}'
    logger.info("CARTO API key loaded successfully")
else:
    logger.warning(
        "CARTO_API_KEY not found in environment. Map tiles will show watermarks. "
        "Get your free API key at https://carto.com/basemaps/apikey and add it to .env"
    )
    CARTO_POSITRON_URL = None

def render_stops_map(stops_data, user_lon, user_lat) -> io.BytesIO | None:
    """Генерирует карту с ближайшими остановками в памяти (BytesIO)."""
    fig, ax = plt.subplots(figsize=(10, 10))
    try:
        # 1. Создаем GeoDataFrame для остановок
        stops_gdf = gpd.GeoDataFrame(
            stops_data,
            geometry=[Point(s['lon'], s['lat']) for s in stops_data],
            crs="EPSG:4326"
        ).to_crs(epsg=3857)
        
        # 2. Отрисовываем остановки (обычные и хабы)
        normal_stops = stops_gdf[stops_gdf['routes_count'] <= 7]
        hub_stops = stops_gdf[stops_gdf['routes_count'] > 7]
        
        if not normal_stops.empty:
            normal_stops.plot(ax=ax, color="blue", markersize=100, alpha=0.6, zorder=3, edgecolor='white')
            
        if not hub_stops.empty:
            hub_stops.plot(ax=ax, color="orange", markersize=150, alpha=0.8, zorder=4, edgecolor='black', linewidth=1)
        
        # 3. Точка пользователя
        user_gdf = gpd.GeoDataFrame(geometry=[Point(user_lon, user_lat)], crs="EPSG:4326").to_crs(epsg=3857)
        user_gdf.plot(ax=ax, color="red", markersize=200, zorder=5, marker='*')
        
        # 4. Подписи для остановок
        for i, stop in stops_gdf.iterrows():
            is_hub = stop['routes_count'] > 7
            color = "orange" if is_hub else "blue"
            weight = "bold" if is_hub else "normal"
            
            ax.annotate(
                stop['title'],
                xy=(stop.geometry.x, stop.geometry.y),
                xytext=(5, 5),
                textcoords="offset points",
                fontsize=9,
                fontweight=weight,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.7)
            )

        # 5. Подложка
        if CARTO_POSITRON_URL:
            cx.add_basemap(ax, source=CARTO_POSITRON_URL, attribution=CARTO_ATTRIBUTION)
        else:
            cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
        ax.axis("off")
        
        # 6. Легенда
        legend_handles = [
            mlines.Line2D([], [], color='red', marker='*', linestyle='None', markersize=15, label='Вы здесь'),
            mlines.Line2D([], [], color='orange', marker='o', linestyle='None', markersize=12, label='Узлы (>7 маршрутов)'),
            mlines.Line2D([], [], color='blue', marker='o', linestyle='None', markersize=10, alpha=0.6, label='Остановки'),
        ]
        ax.legend(
            handles=legend_handles, 
            loc='upper center', 
            bbox_to_anchor=(0.5, -0.01), 
            ncol=3, 
            frameon=True, 
            shadow=True, 
            fontsize=10
        )
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2, dpi=120)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка при создании карты остановок: {e}", exc_info=True)
        return None
    finally:
        plt.close(fig)
        plt.clf()

def render_full_route_map(route_info) -> io.BytesIO | None:
    """Генерирует карту всего маршрута с хабами в памяти (BytesIO)."""
    fig, ax = plt.subplots(figsize=(14, 14))
    try:
        # 1. Отрисовываем линию маршрута
        line_gdf = gpd.GeoDataFrame(index=[0], crs="EPSG:4326", geometry=[shape(json.loads(route_info['geojson']))])
        line_3857 = line_gdf.to_crs(epsg=3857)
        line_3857.plot(ax=ax, linewidth=5, color="blue", alpha=0.4, zorder=2)
        
        # 2. Отрисовываем все остановки
        stops_data = route_info['stops']
        stops_gdf = gpd.GeoDataFrame(
            geometry=[Point(s['lon'], s['lat']) for s in stops_data],
            crs="EPSG:4326"
        ).to_crs(epsg=3857)
        stops_gdf.plot(ax=ax, color="blue", markersize=40, alpha=0.4, zorder=3)
        
        # 3. Помечаем специальные остановки (начало, конец, хабы)
        num_stops = len(stops_data)
        for i, stop in enumerate(stops_data):
            is_hub = stop['routes_at_stop'] > 7
            is_start = (i == 0)
            is_end = (i == num_stops - 1)
            
            if is_start or is_end or is_hub:
                point_3857 = stops_gdf.iloc[i].geometry
                color = "red" if (is_start or is_end) else "orange"
                label = stop['title']
                
                ax.plot(point_3857.x, point_3857.y, marker='o', color=color, markersize=10, zorder=5, markeredgecolor='white')
                
                ax.annotate(
                    label, 
                    xy=(point_3857.x, point_3857.y), 
                    xytext=(5, 5), 
                    textcoords="offset points",
                    fontsize=8,
                    fontweight='bold',
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.7)
                )

        # 4. Подложка
        if CARTO_POSITRON_URL:
            cx.add_basemap(ax, source=CARTO_POSITRON_URL, attribution=CARTO_ATTRIBUTION)
        else:
            cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
        ax.axis("off")
        ax.set_title(f"Маршрут {route_info['num']}: {route_info['title']}", fontsize=12, pad=20)
        
        # 5. Легенда
        legend_handles = [
            mlines.Line2D([], [], color='red', marker='o', linestyle='None', markersize=10, label='Конечные'),
            mlines.Line2D([], [], color='orange', marker='o', linestyle='None', markersize=10, label='Пересадки'),
            mlines.Line2D([], [], color='blue', marker='o', linestyle='None', markersize=8, alpha=0.4, label='Остановки'),
        ]
        ax.legend(handles=legend_handles, loc='upper right', frameon=True, shadow=True, fontsize=10)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.1, dpi=120)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка при создании полной карты маршрута: {e}", exc_info=True)
        return None
    finally:
        plt.close(fig)
        plt.clf()

def render_route_map(route_data) -> io.BytesIO | None:
    """Генерирует изображение карты с участком маршрута (для поиска А-Б) в памяти (BytesIO)."""
    fig, ax = plt.subplots(figsize=(12, 12))
    try:
        all_geoms = []
        legend_handles = []
        
        for idx, segment in enumerate(route_data['mapping_data']):
            color = ROUTE_COLORS[idx % len(ROUTE_COLORS)]
            bus_num = segment['bus_num']
            
            line_json = get_route_segment_geom(segment['route_id'], segment['start_id'], segment['end_id'])
            if line_json:
                line_gdf = gpd.GeoDataFrame(index=[0], crs="EPSG:4326", geometry=[shape(json.loads(line_json))])
                line_3857 = line_gdf.to_crs(epsg=3857)
                line_3857.plot(ax=ax, linewidth=6, color=color, alpha=0.5, zorder=2)
                all_geoms.append(line_3857)
                legend_handles.append(mlines.Line2D([], [], color=color, alpha=0.5, linewidth=4, label=f"Автобус {bus_num}"))
            
            stops_geoms = get_intermediate_stops_geom(segment['route_id'], segment['start_seq'], segment['end_seq'])
            if stops_geoms:
                stops_gdf = gpd.GeoDataFrame(geometry=[shape(s) for s in stops_geoms], crs="EPSG:4326")
                stops_3857 = stops_gdf.to_crs(epsg=3857)
                stops_3857.plot(ax=ax, color=color, markersize=50, alpha=0.5, zorder=3)
                all_geoms.append(stops_3857)

        user_start = route_data['user_coords']['start']
        user_end = route_data['user_coords']['end']
        user_gdf = gpd.GeoDataFrame(geometry=[Point(user_start[1], user_start[0]), Point(user_end[1], user_end[0])], crs="EPSG:4326").to_crs(epsg=3857)
        user_gdf.plot(ax=ax, color="red", markersize=150, zorder=6, edgecolor='white', linewidth=2)
        legend_handles.append(mlines.Line2D([], [], color="red", marker='o', linestyle='None', markersize=10, label="Старт / Финиш"))

        if route_data['type'] == 'transfer':
            t1_lat, t1_lon = route_data['transfer_coords']
            t2_lat, t2_lon = route_data['transfer2_coords']
            trans_gdf = gpd.GeoDataFrame(geometry=[Point(t1_lon, t1_lat), Point(t2_lon, t2_lat)], crs="EPSG:4326").to_crs(epsg=3857)
            trans_gdf.plot(ax=ax, color="orange", markersize=150, zorder=5, edgecolor='black', linewidth=1)
            
            for i, (lat, lon, title) in enumerate([(t1_lat, t1_lon, route_data['transfer_stop']), (t2_lat, t2_lon, route_data['transfer2_stop'])]):
                point_3857 = trans_gdf.iloc[i].geometry
                ax.annotate(title, xy=(point_3857.x, point_3857.y), xytext=(5, 5), textcoords="offset points", fontsize=9, fontweight='bold', bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="orange", alpha=0.8))

            if route_data.get('walk_dist', 0) > 10:
                walk_geom = LineString([[t1_lon, t1_lat], [t2_lon, t2_lat]])
                walk_gdf = gpd.GeoDataFrame(geometry=[walk_geom], crs="EPSG:4326").to_crs(epsg=3857)
                walk_gdf.plot(ax=ax, linewidth=3, color="orange", linestyle="--", alpha=0.9, zorder=4)
                legend_handles.append(mlines.Line2D([], [], color="orange", linestyle='--', linewidth=2, label=f"Переход ({route_data['walk_dist']:.0f}м)"))

        if CARTO_POSITRON_URL:
            cx.add_basemap(ax, source=CARTO_POSITRON_URL, attribution=CARTO_ATTRIBUTION)
        else:
            cx.add_basemap(ax, source=cx.providers.CartoDB.Positron)
        ax.axis("off")
        ax.legend(handles=legend_handles, loc='upper left', frameon=True, shadow=True, fontsize=10)
        
        if all_geoms:
            full_gdf = gpd.pd.concat(all_geoms)
            minx, miny, maxx, maxy = full_gdf.total_bounds
            dx, dy = (maxx - minx) * 0.15, (maxy - miny) * 0.15
            ax.set_xlim(minx - dx, maxx + dx)
            ax.set_ylim(miny - dy, maxy + dy)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.1, dpi=120)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка при создании карты отрезка маршрута: {e}", exc_info=True)
        return None
    finally:
        plt.close(fig)
        plt.clf()
