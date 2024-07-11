"""
/***************************************************************************
 FireToolbox
                                 A QGIS plugin
 A collection of fire insights related algorithms
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2023-08-30
        copyright            : (C) 2024 by Diego Teran - Fire2a.com
        email                : FIX-ME
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

"""
TODO:
- Ver lo del initAlgorithm con Fernando
- Ver bien el tema de las direcciones que quedan guardadas las imagenes y cicatrices (tal vez sea buena idea una carpeta de carpetas)
- Usar el modelo desde el data lake (listo, faltaría ver bien las carpetas dentro del bucket)
- Ver bien cual sería la mejora forma de que se guarden las cosas en el data lake 
- ver como se va a actualizar el data lake, lambda ? 
"""


from fire2a.raster import get_rlayer_data
import os
from qgis.core import (QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterRasterDestination,
                       QgsProject, QgsRasterLayer, QgsProcessingException)
from qgis.PyQt.QtCore import QCoreApplication

import boto3
from .firescarmapping.model_u_net import model, device  # Importar el modelo y dispositivo necesarios
from .firescarmapping.as_dataset import create_datasetAS
from .firescarmapping.bucketdisplay import S3SelectionDialog
import numpy as np
from torch.utils.data import DataLoader
from osgeo import gdal, osr
import torch

class FireScarMapper(QgsProcessingAlgorithm):
    S3_BUCKET = "fire2a-firescars"
    AWS_ACCESS_KEY_ID = "<AWS_ACCESS_KEY_ID>"
    AWS_SECRET_ACCESS_KEY = "<AWS_SECRET_ACCESS_KEY>"

    def download_file_from_s3(self, bucket_name, file_name, local_path):
        s3 = boto3.client(
            's3',
            aws_access_key_id=self.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=self.AWS_SECRET_ACCESS_KEY,
            region_name="us-east-1"
        )
        try:
            s3.download_file(bucket_name, file_name, local_path)
        except Exception as e:
            raise QgsProcessingException(f"Failed to download {file_name} from S3: {str(e)}")
    
    def initAlgorithm(self, config):
        pass
    
    def processAlgorithm(self, parameters, context, feedback):
        # Abrir el diálogo de selección de S3 para localidades
        dialog_locality = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY)
        if dialog_locality.exec_():
            selected_locality = dialog_locality.get_selected_item()  # Debería contener la ruta de la localidad seleccionada en S3

            if not selected_locality.endswith('/'):
                selected_locality += '/'  # Asegurarse de que la ruta de la localidad termine con '/'

            # Abrir el diálogo de selección de S3 para pares de fotos dentro de la localidad
            dialog_pairs = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix=selected_locality)
            if dialog_pairs.exec_():
                selected_pair_folder = dialog_pairs.get_selected_item()  # Debería contener la ruta del par de fotos seleccionado en S3
                feedback.pushDebugInfo(f"selected_pair_folder: {selected_pair_folder}")
                # Construir las rutas de los archivos basados en la selección
                local_path_before = os.path.join(os.path.dirname(__file__), "results",f"{selected_pair_folder.split('/')[0]}-{selected_pair_folder.split('/')[1]}-ImgPre.tif")
                local_path_burnt = os.path.join(os.path.dirname(__file__), "results",f"{selected_pair_folder.split('/')[0]}-{selected_pair_folder.split('/')[1]}-ImgPost.tif")
                
                # Listar los archivos dentro del par de fotos seleccionado
                s3 = boto3.client(
                    's3',
                    aws_access_key_id=self.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=self.AWS_SECRET_ACCESS_KEY,
                    region_name="us-east-1"
                )
                paginator = s3.get_paginator('list_objects_v2')
                try:
                    files = []
                    for result in paginator.paginate(Bucket=self.S3_BUCKET, Prefix=selected_pair_folder):
                        if 'Contents' in result:
                            files.extend([obj['Key'] for obj in result['Contents'] if obj['Key'].endswith('.tif')])

                    if len(files) != 2:
                        raise QgsProcessingException("La carpeta seleccionada no contiene exactamente dos archivos TIF.")
                    
                    # Descargar los archivos
                    for file in files:
                        if 'ImgPreF' in file:
                            feedback.pushDebugInfo(f"Downloading file: {file} to {local_path_before}")
                            feedback.pushDebugInfo(f"file name: {file}")
                            self.download_file_from_s3(self.S3_BUCKET, file, local_path_before)
                            before_layer = QgsRasterLayer(local_path_before, f"{file.split('/')[2][:-23]}")
                        elif 'ImgPosF' in file:
                            feedback.pushDebugInfo(f"Downloading file: {file} to {local_path_burnt}")
                            self.download_file_from_s3(self.S3_BUCKET, file, local_path_burnt)
                            burnt_layer = QgsRasterLayer(local_path_burnt, f"{file.split('/')[2][:-23]}")
                except Exception as e:
                    raise QgsProcessingException(f"Failed to list or download files from S3: {str(e)}")
                
                # Cargar las imágenes descargadas como capas raster

                if not before_layer.isValid() or not burnt_layer.isValid():
                    raise QgsProcessingException("Failed to load raster layers from the downloaded images")

                feedback.pushDebugInfo(f"Loaded rasters:\nBefore Raster valid: {before_layer.isValid()}\nBurnt Raster valid: {burnt_layer.isValid()}")
                # descargar modelo

                model_path = os.path.join(os.path.dirname(__file__), 'firescarmapping', 'ep25_lr1e-04_bs16_021__as_std_adam_f01_13_07_x3.model')
                if not os.path.exists(model_path):
                    self.download_file_from_s3(self.S3_BUCKET, "Model/ep25_lr1e-04_bs16_021__as_std_adam_f01_13_07_x3.model", model_path)
                else:
                    feedback.pushDebugInfo(f"Model already exists at {model_path}")


                output_path = os.path.join(os.path.dirname(__file__), "results",f"{selected_pair_folder.split('/')[0]}-{selected_pair_folder.split('/')[1]}-Firescar.tif")
                feedback.pushDebugInfo(f"output path: {output_path}")  

                rasters = [
                    {"type": "before", "id": 0, "qid": before_layer.id(), "name": before_layer.name(), "data": get_rlayer_data(before_layer), "layer": before_layer},
                    {"type": "burnt", "id": 1, "qid": burnt_layer.id(), "name": burnt_layer.name(), "data": get_rlayer_data(burnt_layer), "layer": burnt_layer}
                ]
                feedback.pushDebugInfo(f"layer name: {before_layer.name()}")  

                before_files = [rasters[0]]
                after_files = [rasters[1]]
                before_files_data = [before_files[0]['data']]
                after_files_data = [after_files[0]['data']]

                device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
                model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))

                np.random.seed(3)
                torch.manual_seed(3)

                data_eval = create_datasetAS(before_files_data, after_files_data, mult=1)

                batch_size = 1  # 1 to create diagnostic images, any value otherwise
                all_dl = DataLoader(data_eval, batch_size=batch_size)  #, shuffle=True)

                model.eval()

                for i, batch in enumerate(all_dl):
                    feedback.pushDebugInfo(f"Processing batch {i}")
                    x = batch['img'].float().to(device)
                    feedback.pushDebugInfo(f"Input shape: {x.shape}")
                    output = model(x).cpu()
                    feedback.pushDebugInfo(f"Output shape: {output.shape}")

                    # obtain binary prediction map
                    pred = np.zeros(output.shape)
                    pred[output >= 0] = 1

                    generated_matrix = pred[0][0]

                    if output_path:
                        # Adjust the name of the output path to be unique for each firescar
                        #output_path_with_index = output_path[:-4] + f"_{i+1}.tif"
                        feedback.pushDebugInfo(f"Writing raster to: {output_path}")
                        self.writeRaster(generated_matrix, output_path, context)
                        feedback.pushDebugInfo(f"Adding raster layer: {output_path}")
                        self.addRasterLayer(output_path, before_files[i]['name'], context)

                return {}

        raise QgsProcessingException("No se seleccionó una localidad o par de fotos válidos.")
    
    def writeRaster(self, matrix, file_path, context):
        height, width = matrix.shape

        driver = gdal.GetDriverByName('GTiff')
        raster = driver.Create(file_path, width, height, 1, gdal.GDT_Int16)

        originX = 0
        originY = 0
        pixelWidth = 1
        pixelHeight = 1

        raster.SetGeoTransform((originX, pixelWidth, 0, originY, 0, -pixelHeight))

        spatialReference = osr.SpatialReference()
        spatialReference.ImportFromEPSG(4326)
        raster.SetProjection(spatialReference.ExportToWkt())

        band = raster.GetRasterBand(1)
        band.WriteArray(matrix)

        band.FlushCache()
        raster.FlushCache()
        raster = None

    def addRasterLayer(self, file_path, layer_name, context):
        layer = QgsRasterLayer(file_path, layer_name, "gdal")
        if not layer.isValid():
            raise QgsProcessingException(f"Failed to load raster layer from {file_path}")

        QgsProject.instance().addMapLayer(layer)

    def name(self):
        return "firescarmapper"
    
    def displayName(self):
        return self.tr("Fire Scar Mapper")

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return FireScarMapper()