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
- initAlgorithm con Fernando
- las direcciones que quedan guardadas las imagenes y cicatrices localmenteb(tal vez sea buena idea una carpeta de carpetas)
- como incorporar las credenciales que se pongan
- ver el tema del feedback que se está dando cuando se están ejecutando las cosas (está en "No responde" mientras se está ejecutando)
- ver el color de las bandas de las imagenes 
- ver de agrupar todo en en grupos para las layers
entrega el resultado)
"""


from fire2a.raster import get_rlayer_data
import os
from qgis.core import (QgsProcessingAlgorithm, QgsProject, QgsRasterLayer, QgsProcessingException, QgsLayerTreeLayer, QgsLayerTreeGroup, QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer, QgsRasterBandStats)
from qgis.PyQt.QtCore import QCoreApplication
from PyQt5.QtGui import QColor

import boto3
from .firescarmapping.model_u_net import model, device  # Importar el modelo y dispositivo necesarios
from .firescarmapping.as_dataset import create_datasetAS
from .firescarmapping.bucketdisplay import S3SelectionDialog
import numpy as np
from torch.utils.data import DataLoader
from osgeo import gdal, osr, gdal_array
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
        dialog_locality = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix="Images/")
        if dialog_locality.exec_():
            selected_locality = dialog_locality.get_selected_item()  # Debería contener la ruta de la localidad seleccionada en S3

            if not selected_locality.endswith('/'):
                selected_locality += '/'  # Asegurarse de que la ruta de la localidad termine con '/'

            # Abrir el diálogo de selección de S3 para pares de fotos dentro de la localidad
            dialog_year = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix=selected_locality)
            if dialog_year.exec_():
                selected_year = dialog_year.get_selected_item()  # Debería contener la ruta del par de fotos seleccionado en S3
                feedback.pushDebugInfo(f"selected_year: {selected_year}")

                dialog_month = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix=selected_year)
                if dialog_month.exec_():
                    selected_month = dialog_month.get_selected_item()  # Debería contener la ruta del par de fotos seleccionado en S3
                    feedback.pushDebugInfo(f"selected_month: {selected_month}")

                    dialog_day = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix=selected_month)
                    if dialog_day.exec_():
                        selected_day = dialog_day.get_selected_item()  # Debería contener la ruta del par de fotos seleccionado en S3
                        feedback.pushDebugInfo(f"selected_day: {selected_day}")

                        dialog_pairs = S3SelectionDialog(self.S3_BUCKET, self.AWS_ACCESS_KEY_ID, self.AWS_SECRET_ACCESS_KEY, prefix=selected_day)
                        if dialog_pairs.exec_():
                            selected_pair_folder = dialog_pairs.get_selected_item()  # Debería contener la ruta del par de fotos seleccionado en S3
                            feedback.pushDebugInfo(f"selected_pair_folder: {selected_pair_folder}")

                            # Construir las rutas de los archivos basados en la selección
                            local_path_before = os.path.join(os.path.dirname(__file__), "results", f"{selected_pair_folder.split('/')[1]}-{selected_pair_folder.split('/')[5]}-ImgPre.tif")
                            local_path_burnt = os.path.join(os.path.dirname(__file__), "results", f"{selected_pair_folder.split('/')[1]}-{selected_pair_folder.split('/')[5]}-ImgPost.tif")

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

                            # Descargar modelo
                            model_path = os.path.join(os.path.dirname(__file__), 'firescarmapping', 'ep25_lr1e-04_bs16_021__as_std_adam_f01_13_07_x3.model')
                            if not os.path.exists(model_path):
                                self.download_file_from_s3(self.S3_BUCKET, "Model/ep25_lr1e-04_bs16_021__as_std_adam_f01_13_07_x3.model", model_path)
                            else:
                                feedback.pushDebugInfo(f"Model already exists at {model_path}")

                            # Ruta de salida para la cicatriz generada
                            firescar_outputfile_name = f"{selected_pair_folder.split('/')[1]}-{selected_pair_folder.split('/')[5]}-Firescar.tif"
                            output_path = os.path.join(os.path.dirname(__file__), "results", firescar_outputfile_name)
                            feedback.pushDebugInfo(f"output path: {output_path}")  

                            # Preparar los datos para la evaluación del modelo
                            rasters = [
                                {"type": "before", "id": 0, "qid": before_layer.id(), "name": before_layer.name(), "data": get_rlayer_data(before_layer), "layer": before_layer},
                                {"type": "burnt", "id": 1, "qid": burnt_layer.id(), "name": burnt_layer.name(), "data": get_rlayer_data(burnt_layer), "layer": burnt_layer}
                            ]

                            before_files_data = [rasters[0]['data']]
                            after_files_data = [rasters[1]['data']]

                            # Cargar el modelo y evaluar las cicatrices generadas
                            device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
                            model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))
                            np.random.seed(3)
                            torch.manual_seed(3)
                            data_eval = create_datasetAS(before_files_data, after_files_data, mult=1)
                            batch_size = 1  # 1 para crear imágenes de diagnóstico, cualquier valor de lo contrario
                            all_dl = DataLoader(data_eval, batch_size=batch_size)

                            model.eval()

                            for i, batch in enumerate(all_dl):
                                feedback.pushDebugInfo(f"Processing batch {i}")
                                x = batch['img'].float().to(device)
                                feedback.pushDebugInfo(f"Input shape: {x.shape}")
                                output = model(x).cpu()
                                feedback.pushDebugInfo(f"Output shape: {output.shape}")

                                # Obtener el mapa de predicción binaria
                                pred = np.zeros(output.shape)
                                pred[output >= 0] = 1

                                generated_matrix = pred[0][0]

                                if output_path:
                                    feedback.pushDebugInfo(f"Writing raster to: {output_path}")
                                    # Llamar a la función para escribir el raster georreferenciado
                                    self.writeRaster(generated_matrix, output_path, before_layer, feedback)
                                    feedback.pushDebugInfo(f"Adding raster layer: {output_path}")
                                    group_name = f"{selected_locality.split('/')[1]} - {selected_pair_folder.split('/')[5]}"

                                    # Verificar si el grupo ya existe, si no, crearlo
                                    root = QgsProject.instance().layerTreeRoot()
                                    group = root.findGroup(group_name)
                                    if not group:
                                        group = root.addGroup(group_name)
                                    self.addRasterLayer(local_path_before, f"{selected_pair_folder.split('/')[1]}-{selected_pair_folder.split('/')[5]}-ImgPre", group, context)
                                    self.addRasterLayer(local_path_burnt, f"{selected_pair_folder.split('/')[1]}-{selected_pair_folder.split('/')[5]}-ImgPosF", group, context)
                                    self.addRasterLayer(output_path, firescar_outputfile_name, group, context)
                                    self.add_openstreetmap_layer_if_needed()
                                    

                            return {}

        raise QgsProcessingException("No se seleccionó una localidad o par de fotos válidos.")
    
    def writeRaster(self, matrix, file_path, before_layer, feedback):
        # Obtener las dimensiones del raster antes del incendio
        width = before_layer.width()
        height = before_layer.height()

        # Crear el archivo raster de salida
        driver = gdal.GetDriverByName('GTiff')
        raster = driver.Create(file_path, width, height, 1, gdal.GDT_Float32)

        if raster is None:
            raise QgsProcessingException("Failed to create raster file.")

        # Configurar la geotransformación y proyección
        extent = before_layer.extent()
        pixel_width = extent.width() / width
        pixel_height = extent.height() / height
        raster.SetGeoTransform((extent.xMinimum(), pixel_width, 0, extent.yMaximum(), 0, -pixel_height))
        raster.SetProjection(before_layer.crs().toWkt())

        # Obtener la banda del raster
        band = raster.GetRasterBand(1)

        # Calcular el offset y el tamaño de la región de la cicatriz para ajustar al raster
        # Asegurar que la cicatriz no se recorte más allá de las dimensiones del raster
        start_row = 0
        start_col = 0
        matrix_height, matrix_width = matrix.shape

        if matrix_height > height:
            start_row = (matrix_height - height) // 2
            matrix_height = height
        if matrix_width > width:
            start_col = (matrix_width - width) // 2
            matrix_width = width

        # Recortar la matriz para que coincida con las dimensiones del raster
        resized_matrix = matrix[start_row:start_row + matrix_height, start_col:start_col + matrix_width]

        # Escribir la matriz en la banda del raster de salida
        try:
            gdal_array.BandWriteArray(band, resized_matrix, 0, 0)
        except ValueError as e:
            raise QgsProcessingException(f"Failed to write array to raster: {str(e)}")

        # Establecer el valor NoData si es necesario
        band.SetNoDataValue(0)

        # Limpiar caché y cerrar el raster
        band.FlushCache()
        raster.FlushCache()
        raster = None

        feedback.pushInfo(f"Raster written to {file_path}")


    def addRasterLayer(self, file_path, layer_name, group, context):
        layer = QgsRasterLayer(file_path, layer_name, "gdal")
        if not layer.isValid():
            raise QgsProcessingException(f"Failed to load raster layer from {file_path}")

        QgsProject.instance().addMapLayer(layer, False)
        group.insertChildNode(0, QgsLayerTreeLayer(layer))

    def add_openstreetmap_layer_if_needed(self):
        # Verificar si ya hay una capa OpenStreetMap
        project = QgsProject.instance()
        layers = project.mapLayers().values()
        for layer in layers:
            if layer.name() == "OpenStreetMap":
                print("OpenStreetMap layer already exists.")
                return

        # URL del tile server de OpenStreetMap
        urlWithParams = 'type=xyz&url=http://a.tile.openstreetmap.org/{z}/{x}/{y}.png'
        
        # Crear la capa OpenStreetMap
        layer = QgsRasterLayer(urlWithParams, 'OpenStreetMap', 'wms')
        
        # Verificar si la capa fue creada correctamente
        if not layer.isValid():
            print("Failed to create OpenStreetMap layer")
            return
        
        # Agregar la capa al proyecto
        project.addMapLayer(layer)
        print("OpenStreetMap layer added.")

    def name(self):
        return "firescarmapper"
    
    def displayName(self):
        return self.tr("Fire Scar Mapper")

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return FireScarMapper()