# 此间 · 城市图静态资源

散步城市图用的两种静态文件：城市目录分片与城市地图包。App 只按固定路径下载它们，
归属判定在手机上完成——**没有会接收或处理用户坐标的服务器**。

- 索引与全部文件清单：<https://hereat.github.io/city-atlas/>
- 生成这些文件的全部脚本：[`pipeline/`](pipeline/)

## 路径

```
directory/latest.json                        当前目录版本
directory/v{n}/{lat}_{lon}.json.gz           1°×1° 的目录分片，两个数是格西南角的整数纬经度（负数带号，如 45_-123）
package/{cityID}/{mapDataVersion}.json.gz    一座城的地图包
```

## 许可

地图数据 © OpenStreetMap contributors，按 [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/) 提供。
这些文件是从 OSM 数据裁剪、简化得到的**衍生数据库**；本仓库同时提供衍生数据库本身与生成方法。

城市画框依据 [GHSL Urban Centre Database R2024A](https://human-settlement.emergency.copernicus.eu/ghs_ucdb_2024.php)，
© European Union，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 提供。
