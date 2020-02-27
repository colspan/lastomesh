
#!/bin/env python
# -*- coding: utf-8 -*-
import json
import luigi
import os
import requests
import subprocess

import numpy as np
import open3d as o3d
from lasto3dtiles.format.las import LasFile
from lasto3dtiles.format.ply import PlyFile


class TextDownloader(luigi.Task):
    filepath = luigi.Parameter()
    url = luigi.Parameter()
    decode = luigi.Parameter(default='utf-8')

    def output(self):
        return luigi.LocalTarget(
            format=luigi.format.UTF8, path=self.filepath)

    def run(self):
        r = requests.get(self.url)
        with self.output().open('w') as f:
            f.write(r.content.decode(self.decode))


class BinaryDownloader(luigi.Task):
    filepath = luigi.Parameter()
    url = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(
            format=luigi.format.Nop, path=self.filepath)

    def run(self):
        r = requests.get(self.url)
        with self.output().open('w') as f:
            f.write(r.content)


class ConvertLasFile(luigi.Task):
    input_path = luigi.Parameter()
    output_path = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(
            format=luigi.format.Nop, path=self.output_path)

    def run(self):
        cmd = [
            'las2las',
            '-f',
            '1.0',
            self.input_path,
            self.output_path,
        ]
        subprocess.check_output(cmd)


class CreateMeshFromLasData(luigi.Task):
    product_id = luigi.Parameter()
    base_url = 'https://raw.githubusercontent.com/colspan/pcd-open-datasets/master/shizuokapcd/product/{}.json'
    output_dir = luigi.Parameter(default='tmp/mesh')
    output_filename = luigi.Parameter(default=None)
    work_dir = luigi.Parameter(default='tmp/work')
    file_format = luigi.Parameter(default='ply')
    mesh_type = luigi.Parameter(default='poisson')
    simplify_type = luigi.Parameter(default=None)

    def requires(self):
        return TextDownloader(
            url=self.base_url.format(self.product_id),
            filepath=os.path.join(self.work_dir, '{}.json'.format(self.product_id)))

    def output(self):
        output_filename = 'mesh-{}.{}'.format(self.product_id, self.file_format) \
            if self.output_filename is None \
            else self.output_filename
        return {
            'mesh_file': luigi.LocalTarget(os.path.join(
                self.output_dir, output_filename)),
            'stat_info': luigi.LocalTarget(os.path.join(
                self.output_dir, 'stat-{}.json'.format(self.product_id))),
        }

    def run(self):
        # load metadata
        with self.input().open('r') as f:
            product_info = json.load(f)
        las_urls = product_info['lasUrls']['value']

        # get las dataset
        download_tasks = [
            BinaryDownloader(
                url=x,
                filepath=os.path.join(self.work_dir, os.path.basename(x)))
            for x in las_urls
        ]
        yield download_tasks

        # convert las files
        convert_tasks = []
        for download_task in download_tasks:
            original_path = os.path.basename(download_task.output().path)
            converted_path = os.path.join(
                self.work_dir, 'converted-{}'.format(os.path.basename(original_path)))
            convert_tasks.append(
                ConvertLasFile(
                    input_path=download_task.output().path,
                    output_path=converted_path
                ))
        yield convert_tasks

        # load dataset
        lasdataset = []
        for convert_task in convert_tasks:
            lasdataset.append(
                LasFile(convert_task.output().path).toarray(skip_rate=0.5))

        lasdata = np.concatenate(lasdataset)

        plydata = PlyFile(data=lasdata)
        pcd = plydata.obj

        # fetch stat
        distances = o3d.geometry.PointCloud.compute_nearest_neighbor_distance(
            pcd)
        avg_dist = np.mean(distances)
        stat_info = {
            'id': self.product_id,
            'shape': lasdata.shape,
            'average_distance': avg_dist,
        }

        with self.output()['stat_info'].open('w') as f:
            json.dump(stat_info, f)

        # 指定したvoxelサイズでダウンサンプリング
        voxel_size = avg_dist * 3
        voxel_down_pcd = o3d.geometry.PointCloud.voxel_down_sample(
            pcd, voxel_size=voxel_size)
        target_pcd = voxel_down_pcd

        # 法線計算
        target_pcd.estimate_normals(
            o3d.geometry.KDTreeSearchParamHybrid(
                radius=voxel_size,
                max_nn=30))
        # target_pcd, _ = target_pcd.remove_statistical_outlier(5, 1.5)
        # target_pcd.orient_normals_to_align_with_direction()
        target_pcd.orient_normals_towards_camera_location([0, 0, 10000])
        target_pcd = target_pcd.normalize_normals()

        # メッシュ化
        if self.mesh_type == 'ball-pivoting':
            radius = voxel_size
            radii = [
                radius*0.5,
                radius,
                radius*2,
                radius*4,
                radius*8,
                radius*16,
            ]
            mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
                target_pcd, o3d.utility.DoubleVector(radii))
        else:
            mesh, _ = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
                target_pcd, depth=11, linear_fit=True)

        if self.simplify_type == 'quadric-decimation':
            mesh = mesh.simplify_quadric_decimation(lasdata.shape[0]/2)
        elif self.simplify_type == 'vertex-clustering':
            mesh = mesh.simplify_vertex_clustering(self, voxel_size*1.1)
        else:
            pass

        # データ保存
        o3d.io.write_triangle_mesh(self.output()['mesh_file'].path, mesh)


class ShowPointCloud(luigi.Task):
    product_id = luigi.Parameter()

    def requires(self):
        return CreateMeshFromLasData(self.product_id)

    def run(self):
        mesh = o3d.io.read_triangle_mesh(self.input().path)
        # メッシュデータの表示
        o3d.visualization.draw_geometries([mesh])


if __name__ == "__main__":
    luigi.run()
