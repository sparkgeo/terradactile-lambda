import tempfile
from os.path import join, splitext, dirname
from os import listdir, mkdir
import urllib.request
import io
import subprocess
import shutil
from math import log, tan, pi
from itertools import product
import sys
from osgeo import gdal
import boto3
import json
import uuid

print('Loading function')

s3 = boto3.resource("s3")

tile_url = "https://s3.amazonaws.com/elevation-tiles-prod/geotiff/{z}/{x}/{y}.tif"
s3_bucket = "terradactile"

def respond(err, res=None):
    return {
        'statusCode': '400' if err else '200',
        'body': err if err else json.dumps(res),
        'headers': {
            'Content-Type': 'application/json',
            "Access-Control-Allow-Origin" : "https://terradactile.netlify.app"
        },
    }

def download(output_path, tiles, verbose=True):
    ''' Download list of tiles to a temporary directory and return its name.
    '''
    dir = dirname(output_path)
    _, ext = splitext(output_path)
    merge_geotiff = bool(ext.lower() in ('.tif', '.tiff', '.geotiff'))

    files = []

    for (z, x, y) in tiles:
        response = urllib.request.urlopen(tile_url.format(z=z, x=x, y=y))
        if response.getcode() != 200:
            raise RuntimeError('No such tile: {}'.format((z, x, y)))
        if verbose:
            print('Downloaded', response.url, file=sys.stderr)
        with io.open(join(dir, '{}-{}-{}.tif'.format(z, x, y)), 'wb') as file:
            file.write(response.read())
            files.append(file.name)

    if merge_geotiff:
        if verbose:
            print('Combining', len(files), 'into', output_path, '...', file=sys.stderr)
        vrt = gdal.BuildVRT("/tmp/mosaic.vrt", files)
        opts = gdal.WarpOptions(format="GTiff")
        mosaic = gdal.Warp(output_path, vrt, options=opts)
        mosaic.FlushCache()
        mosaic = None
    else:
        if verbose:
            print('Moving', dir, 'to', output_path, '...', file=sys.stderr)
        shutil.move(dir, output_path)

            
def mercator(lat, lon, zoom):
    ''' Convert latitude, longitude to z/x/y tile coordinate at given zoom.
    '''
    # convert to radians
    x1, y1 = lon * pi/180, lat * pi/180

    # project to mercator
    x2, y2 = x1, log(tan(0.25 * pi + 0.5 * y1))

    # transform to tile space
    tiles, diameter = 2 ** zoom, 2 * pi
    x3, y3 = int(tiles * (x2 + pi) / diameter), int(tiles * (pi - y2) / diameter)

    return zoom, x3, y3

def tiles(zoom, lat1, lon1, lat2, lon2):
    ''' Convert geographic bounds into a list of tile coordinates at given zoom.
    '''
    # convert to geographic bounding box
    minlat, minlon = min(lat1, lat2), min(lon1, lon2)
    maxlat, maxlon = max(lat1, lat2), max(lon1, lon2)

    # convert to tile-space bounding box
    _, xmin, ymin = mercator(maxlat, minlon, zoom)
    _, xmax, ymax = mercator(minlat, maxlon, zoom)

    # generate a list of tiles
    xs, ys = range(xmin, xmax+1), range(ymin, ymax+1)
    tiles = [(zoom, x, y) for (y, x) in product(ys, xs)]
    
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
    print(f"EVENT: {event}")
    
    body = json.loads(event['body'])
    print(f"BODY: {body}")
    
    req_tiles = tiles(
        body.get("z"),
        body.get("y1"),
        body.get("x1"),
        body.get("y2"),
        body.get("x2"),
    )
    
    s3_folder = str(uuid.uuid4())
    mkdir(f"/tmp/{s3_folder}")
    
    mosaic_path = f'/tmp/{s3_folder}/mos.tif'

    tile_limit = 50
    print(f"REQUESTING: {len(req_tiles)} tiles")
    if len(req_tiles) > tile_limit:
        return respond(f"Requested too many tiles ({len(req_tiles)} in total, limit is {tile_limit}). Try a lower zoom level or smaller bbox.")
    else:
        download(mosaic_path, req_tiles)
    
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

    return respond(None, f"s3://{s3_bucket}/{s3_folder}")
