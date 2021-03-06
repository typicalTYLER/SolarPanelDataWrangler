import argparse
import json
import math
import os
import sys
import time
import solardb

import geojson
import geojsonio as geojsonio
import geopandas
import numpy as np
from shapely.geometry import shape, mapping, GeometryCollection, Point

from gather_city_shapes import get_city_state_filepaths, get_city_state_tuples


def deg2num(arr, zoom=21):
    """
    Convert input array of longitude and latitude into slippy tile coordinates.

    :param arr: input list or tuple that contains the longitude and latitude to convert, the input is in addressable
    form so that it can be called by np.apply_along_axis(...)
    :param zoom: zoom parameter necessary for calculating slippy tile coordinates, defaults to 21 because that's the
    zoom level DeepSolar operates at
    :return: slippy tile coordinate tuple containing column and row (sometimes referred to as x_tile and y_tile
    respectively)
    """
    lon_deg = arr[0]
    lat_deg = arr[1]
    lat_rad = np.math.radians(lat_deg)
    n = 2.0 ** zoom
    column = int((lon_deg + 180.0) / 360.0 * n)
    row = int((1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
    return column, row


def num2deg(arr, zoom=21, center=True):
    """
    Convert input array of column and row into longitude latitude coordinates

    :param arr: input list or tuple that contains slippy tile column and row coordinates to convert. As above, the input
    is in addressable form so that np.apply_along_axis(...) can easily call it.
    :param zoom: zoom parameter necessary for calculating longitude latitude coordinates, defaults to 21 because that's
    the zoom level DeepSolar operates at
    :param center: boolean that determines whether the lon_lat should be at the middle of the tile or the top left,
    defaults to center
    :return: lon lat tuple
    """
    column = arr[0]
    row = arr[1]
    if center:
        column += 0.5
        row += 0.5
    n = 2.0 ** zoom
    lon_deg = column / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * row / n)))
    lat_deg = math.degrees(lat_rad)
    return lon_deg, lat_deg


def get_polygons(csvpath, exclude=None):
    """
    Loads polygons for cities listed in csvpath, polygons must have already been calculated and placed into
    data/geoJSON/<city>.<state>.json

    :param csvpath: path to csv containing city, state names for polygons to load
    :param exclude: list of name strings to exclude from the load (name string is "<city>, <state>"), default None
    :return: yields the json of the polygon file
    """
    if exclude is None:
        exclude = []
    for city, state, filepath in get_city_state_filepaths(csvpath):
        name_string = ", ".join((city, state))
        if name_string not in exclude:
            with open(filepath, 'r') as infile:
                yield json.load(infile)


def combine_all_polygons(csvpath, exclude=None):
    """
    Combines all polygons loaded from csvpath together into one GeometryCollection, after running some simplification on
    them to decrease computational complexity.

    :param csvpath: path to csv containing city, state names for polygons to load
    :param exclude: list of name strings to exclude from the load (name string is "<city>, <state>"), default None
    :return: GeometryCollection containing all simplified polygons loaded from files
    """
    return GeometryCollection([simplify_polygon(polygon) for polygon in get_polygons(
        csvpath, exclude=exclude)])


def simplify_polygon(polygon, simplify_tolerance=0.001, buffer_distance=0.004):
    """
    Copies the given polygon and runs simplification on it to reduce computational complexity. Iirc, the parameter
    defaults are taken from some service that simplifies polygons.

    :param polygon: Input polygon
    :param simplify_tolerance: parameter to pass to simplify, specifies how close the simplified coordinates have to be
    to the original
    :param buffer_distance: distance to "dilate" the polygon, the default parameter grows it somewhat
    :return: simplified shapely polygon
    """
    return shape(polygon).convex_hull.simplify(simplify_tolerance).buffer(buffer_distance)


def convert_to_slippy_tile_coords(polygons, zoom=21):
    """
    Converts multiple polygons into slippy tile coordinates

    :param polygons: polygons to convert
    :param zoom: zoom level used in conversion, defaults to 21
    :return: converted polygons
    """
    converted_polygons = []
    for polygon in polygons:
        geojson_object = mapping(polygon)
        geojson_object['coordinates'] = np.apply_along_axis(deg2num, 2, np.array(geojson_object['coordinates']),
                                                            zoom=zoom)
        converted_polygons.append(shape(geojson_object))
    return converted_polygons


def save_geojson(filename, feature):
    """
    Saves the feature parameter in a proper geoJSON file at the given filename in the data directory

    :param filename: filename of saved file in ./data/<filename>
    :param feature: geoJSON feature to save (usually polygon)
    """
    with open(os.path.join('data', filename), 'w') as outfile:
        geojson.dump(geojson.Feature(geometry=feature, properties={}), outfile)


def point_mapper(x, polygon=None):
    """
    Simple function required to call apply_along_axis and get a boolean mask

    :param x: tuple/list point
    :param polygon: polygon to check if point is contained in
    :return: whether or not the polygon contains the point
    """
    return not polygon.contains(Point((x[0], x[1])))


def get_coords_inside_polygon(polygon):
    """
    Calculate all grid coordinate inside a given polygon.

    This method takes a while, possibly need to multiprocess (1 cpu is maxed out on my machine) or switch to matplotlib
    for possibly more efficient code:
    https://stackoverflow.com/questions/21339448/how-to-get-list-of-points-inside-a-polygon-in-python

    :param polygon: polygon to calculate coordinates inside of
    :return: ndarray containing coordinate pairs (shape (x,2))
    """
    # get a meshgrid the size of the polygon's bounding box
    x, y = np.meshgrid(np.arange(polygon.bounds[0], polygon.bounds[2]), np.arange(polygon.bounds[1], polygon.bounds[3]))

    # convert the meshgrid to an array of points
    x, y = x.flatten(), y.flatten()
    points = np.vstack((x, y)).T

    # calculate if the polygon contains every point
    mask = np.apply_along_axis(point_mapper, 1, points, polygon=polygon)

    # stack the mask so each boolean value gets propagated to both coords
    mask = np.stack((mask, mask), axis=1)

    # delete the points outside the polygon and return
    return np.ma.masked_array(points, mask=mask).compressed().reshape((-1, 2))


def get_coords_caller(name, polygon):
    """
    Calls the get inner coordinate method and times the execution

    :param name: name of polygon
    :param polygon: polygon to calculate inner coordinates of
    :return: ndarray containing coordinate pairs (shape (x,2))
    """
    start_time = time.time()
    coordinates = get_coords_inside_polygon(polygon)
    print(str(time.time() - start_time) + " seconds to complete inner grid calculations for " + name)
    return coordinates


def calculate_inner_coordinates_from_csvpath(csvpath, zoom=21):
    """
    Calculates and persists inner coordinates of all polygons in csvpath, this is the public api

    :param csvpath: path containing the csv file for all polygons to calculate inner coordinates for
    :param zoom: zoom level at which to calculate inner coordinates, defaults to 21
    """
    start = time.time()

    polygons = list(combine_all_polygons(csvpath, exclude=solardb.get_inner_coords_calculated_polygon_names()))
    city_state_tuples = list(get_city_state_tuples(csvpath))
    polygon_names = [', '.join(city_state_tuple) for city_state_tuple in city_state_tuples]
    calculate_inner_coordinates(polygon_names, polygons, zoom)
    print("Total running time to calculate inner coordinates: " + str(time.time() - start) + " seconds.")


def calculate_inner_coordinates(polygon_names, polygons, zoom=21):
    slippy_tile_coordinate_polygons = list(convert_to_slippy_tile_coords(polygons, zoom=zoom))
    assert (len(polygon_names) == len(slippy_tile_coordinate_polygons))  # make sure no length mismatch
    zipped_names_and_polygons = list(zip(polygon_names, slippy_tile_coordinate_polygons))
    solardb.persist_polygons(zipped_names_and_polygons, zoom=zoom)
    to_calculate_names_and_polygons = []
    for name, polygon in zipped_names_and_polygons:
        if not solardb.polygon_has_inner_grid(name):
            to_calculate_names_and_polygons.append((name, polygon))
    for name, polygon in to_calculate_names_and_polygons:
        coordinates = get_coords_caller(name, polygon)
        solardb.persist_coords(name, coordinates, zoom=zoom)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=
                                     'Process shapes of city polygons that were created by gather_city_shapes.py')
    parser.add_argument('--input_csv', dest='csvpath', default=os.path.join('data', '100k_US_cities.csv'),
                        help='specify the csv list of city and state names to gather geoJSON for')
    parser.add_argument('--combine_polygons', dest='combine_polygons', action='store_const',
                        const=True, default=False,
                        help='Combine all of the city polygons and save into a geojson')
    parser.add_argument('--calculate_area', dest='area', action='store_const',
                        const=True, default=False,
                        help='Calculates the area of all polygons in km2')
    parser.add_argument('--calculate_inner_grid', dest='inner', action='store_const',
                        const=True, default=False,
                        help='Calculates every slippy coordinate that\'s within a polygon, '
                             'currently takes a very long time')
    parser.add_argument('--calculate_centroids', dest='centroids', action='store_const',
                        const=True, default=False,
                        help='Calculates missing centroids in the database')
    parser.add_argument('--query_osm_solar', dest='osm_solar', action='store_const',
                        const=True, default=False,
                        help='Queries and persists existing solar panel locations from osm from the combined polygons')
    parser.add_argument('--geojsonio', dest='geojsonio', action='store_const',
                        const=True, default=False,
                        help='Opens processing output in geojsonio if the operation makes sense')
    args = parser.parse_args()

    output = None
    if args.combine_polygons:
        geometry_collection_of_polygons = combine_all_polygons(args.csvpath)
        save_geojson('geom_collection.geojson', geometry_collection_of_polygons)
        output = geometry_collection_of_polygons
    if args.area:
        projected_polygons = convert_to_slippy_tile_coords(list(combine_all_polygons(args.csvpath)), zoom=21)
        print(str(math.ceil(sum([polygon.area for polygon in projected_polygons])))
              + " total tiles at zoom level " + str(21) + " in this multipolygon area!")
        output = projected_polygons
    if args.inner:
        calculate_inner_coordinates_from_csvpath(csvpath=args.csvpath, zoom=21)
    if args.centroids:
        solardb.compute_centroid_distances()
    if args.osm_solar:
        solardb.query_and_persist_osm_solar(list(combine_all_polygons(args.csvpath)))
    if args.geojsonio and output is not None:
        geojsonio.display(geopandas.GeoSeries(output))
