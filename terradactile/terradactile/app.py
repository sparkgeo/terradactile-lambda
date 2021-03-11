import tempfile
from os.path import join, splitext, dirname
from os import listdir, mkdir, environ
import urllib.request
import io
import shutil
from math import log, tan, pi
from itertools import product
import sys
import boto3
import json
import uuid
import csv
from pyproj import Proj, transform
from osgeo import gdal

s3 = boto3.resource("s3")

tile_url = "https://s3.amazonaws.com/elevation-tiles-prod/v2/geotiff/{z}/{x}/{y}.tif"
s3_bucket = environ.get("BUCKET")

def respond(err, res=None, origin=None):
    return {
        'statusCode': '400' if err else '200',
        'body': err if err else json.dumps(res),
        'headers': {
            'Content-Type': 'application/json',
            "Access-Control-Allow-Origin" : origin,
            'Access-Control-Allow-Methods': 'OPTIONS,POST,GET',
            'Access-Control-Allow-Headers': 'Content-Type'
        },
    }

def reproject_point(x1, y1, in_epsg, out_epsg):
    inProj = Proj(f'epsg:{in_epsg}')
    outProj = Proj(f'epsg:{out_epsg}')
    return transform(inProj, outProj, x1, y1, always_xy=True)

def download(output_path, tiles, clip_bounds, verbose=True):
    ''' Download list of tiles to a temporary directory and return its name.
    '''
    dir = dirname(output_path)
    _, ext = splitext(output_path)
    merge_geotiff = bool(ext.lower() in ('.tif', '.tiff', '.geotiff'))

    files = []

    for (z, x, y) in tiles:
        try:
            response = urllib.request.urlopen(tile_url.format(z=z, x=x, y=y))
            if response.getcode() != 200:
                print(('No such tile: {}'.format((z, x, y))))
                pass
            if verbose:
                print('Downloaded', response.url, file=sys.stderr)
            with io.open(join(dir, '{}-{}-{}.tif'.format(z, x, y)), 'wb') as file:
                file.write(response.read())
                files.append(file.name)
        except urllib.error.URLError as e:
            ResponseData = e.read().decode("utf8", 'ignore')
            print(f"ERROR: {ResponseData}")

    if merge_geotiff:
        if verbose:
            print('Combining', len(files), 'into', output_path, '...', file=sys.stderr)
        vrt = gdal.BuildVRT("/tmp/mosaic.vrt", files)
        
        minX, minY = reproject_point(clip_bounds[0], clip_bounds[1], 4326, 3857)
        maxX, maxY = reproject_point(clip_bounds[2], clip_bounds[3], 4326, 3857)
        wkt = f"POLYGON(({minX} {maxY}, {maxX} {maxY}, {maxX} {minY}, {minX} {minY}, {minX} {maxY}))"

        out_csv = "/tmp/out.csv"
        with open(out_csv, "w") as f:
            fieldnames = ['id', 'wkt']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({'id': 1, 'wkt': wkt})
        
        mosaic = gdal.Warp(output_path, vrt, cutlineDSName=out_csv, cropToCutline=True, format="GTiff")
        mosaic.FlushCache()
        mosaic = None
    else:
        if verbose:
            print('Moving', dir, 'to', output_path, '...', file=sys.stderr)
        shutil.move(dir, output_path)

            
def mercator(lat, lon, zoom):
    x1, y1 = lon * pi/180, lat * pi/180
    x2, y2 = x1, log(tan(0.25 * pi + 0.5 * y1))
    tiles, diameter = 2 ** zoom, 2 * pi
    x3, y3 = int(tiles * (x2 + pi) / diameter), int(tiles * (pi - y2) / diameter)
    return x3, y3

def tiles(z, minX, minY, maxX, maxY):
    xmin, ymin = mercator(maxY, minX, z)
    xmax, ymax = mercator(minY, maxX, z)
    xs, ys = range(xmin, xmax+1), range(ymin, ymax+1)
    tiles = [(z, x, y) for (y, x) in product(ys, xs)]
    return tiles
    
def tif_to_cog(input_tif, output_cog):
    data = gdal.Open(input_tif)
    data_geotrans = data.GetGeoTransform()
    data_proj = data.GetProjection()
    data_array = data.ReadAsArray()
    
    x_size = data.RasterXSize
    y_size = data.RasterYSize
    num_bands = data.RasterCount
    datatype = data.GetRasterBand(1).DataType
    data = None
    
    driver = gdal.GetDriverByName('MEM')
    data_set = driver.Create('', x_size, y_size, num_bands, datatype)

    for i in range(num_bands):
        data_set_lyr = data_set.GetRasterBand(i + 1)
        if len(data_array.shape) == 2:
            data_set_lyr.WriteArray(data_array)
        else:
            data_set_lyr.WriteArray(data_array[i])

    data_set.SetGeoTransform(data_geotrans)
    data_set.SetProjection(data_proj)
    data_set.BuildOverviews("NEAREST", [2, 4, 8, 16, 32, 64])
    
    driver = gdal.GetDriverByName('GTiff')
    data_set2 = driver.CreateCopy(
        output_cog,
        data_set,
        options = [
                "COPY_SRC_OVERVIEWS=YES",
                "TILED=YES",
                "COMPRESS=LZW"
            ]
        )
    data_set = None
    data_set2 = None

def translate_scale(input_tif, output_tif):
    ds = gdal.Open(input_tif)
    ds = gdal.Translate(
        output_tif,
        ds,
        format='GTiff',
        outputType=1,
        scaleParams=[[]]
    )
    ds = None

def write_to_s3(tmp_path, s3_path):
    s3.meta.client.upload_file(tmp_path, s3_bucket, s3_path)

def make_output(input_cog, output, s3_folder):
    ds = gdal.Open(input_cog)

    output_path = f'/tmp/{s3_folder}/{output}.tif'
    gdal.DEMProcessing(
        destName=output_path,
        srcDS=ds,
        processing=output,
        format="GTiff",
        zFactor=1,
        scale=1,
        azimuth=315,
        altitude=45
    )

    ds = None
    
    output_cog = f'/tmp/{s3_folder}/{output}_cog.tif'
    tif_to_cog(output_path, output_cog)
    write_to_s3(output_cog, f'{s3_folder}/{output}.tif')

def lambda_handler(event, context):
    origin = event["headers"]["origin"]
    allowed_origins = [x.strip(' ') for x in environ.get("ALLOWED_ORIGINS").split(",")]

    if origin not in allowed_origins:
        return respond("Origin not in allowed origins!", None, "*")
    
    body = json.loads(event['body'])
    
    x1 = body.get("x1")
    x2 = body.get("x2")
    y1 = body.get("y1")
    y2 = body.get("y2")
    z = body.get("z")

    minX = min([x1, x2])
    maxX = max([x1, x2])
    minY = min([y1, y2])
    maxY = max([y1, y2])

    clip_bounds = (minX, minY, maxX, maxY)
    req_tiles = tiles(z, minX, minY, maxX, maxY)
    
    s3_folder = str(uuid.uuid4())
    mkdir(f"/tmp/{s3_folder}")
    
    mosaic_path = f'/tmp/{s3_folder}/mos.tif'

    tile_limit = 50
    if len(req_tiles) > tile_limit:
        return respond(f"Requested too many tiles ({len(req_tiles)} in total, limit is {tile_limit}). Try a lower zoom level or smaller bbox.")
    else:
        download(mosaic_path, req_tiles, clip_bounds)
    
    mosaic_cog = f'/tmp/{s3_folder}/mos_cog.tif'
    tif_to_cog(mosaic_path, mosaic_cog)
    write_to_s3(mosaic_cog, f'{s3_folder}/mosaic.tif')
    
    mosaic_display = f'/tmp/{s3_folder}/mos_display_cog.tif'
    translate_scale(mosaic_cog, mosaic_display)
    write_to_s3(mosaic_display, f'{s3_folder}/mosaic_display.tif')
    
    outputs = body.get("outputs", [])
    outputs.append("hillshade")

    for output in outputs:
        make_output(mosaic_cog, output, s3_folder)

    return respond(None, f"s3://{s3_bucket}/{s3_folder}", origin)
