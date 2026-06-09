import json
import os
import re
import traceback
import urllib.parse

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QColorDialog, QTableWidgetItem, QCheckBox, QWidget,
    QHBoxLayout, QMessageBox, QPushButton, QAbstractItemView
)
from qgis.PyQt.QtGui import QColor, QBrush

from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsWkbTypes,
    QgsLayerTreeLayer,
)

try:
    from qgis.core import QgsVectorTileLayer
except ImportError:
    QgsVectorTileLayer = None


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "dialog.ui")
)

# ===================== オプション ライセンス設定 =====================
# 認証サーバ（PHP）の公開URL。
LICENSE_SERVER_BASE = "https://forestgeo.info/ForestGeoStudio"
# 各オプションが必要とする保護JS
#   オプション1: 微地形表現図など
#   オプション2: 属性集計など
OPTION1_FEATURE_KEYS = ("csmap", "inyouzu", "mpirrim", "cimap", "colorrelief", "twi", "topex", "weather", "kikikuru", "sentinel")
# =====================================================================

THEMES = {
    "緑系": {
        "main": "#007C45",
        "dark": "#005842",
        "text": "#ffffff"
    },
    "青系": {
        "main": "#18448E",
        "dark": "#213A70",
        "text": "#ffffff"
    },
    "灰系": {
        "main": "#4E4449",
        "dark": "#24130D",
        "text": "#ffffff"
    },
    "茶系": {
        "main": "#8E5E4A",
        "dark": "#612C16",
        "text": "#ffffff"
    }

}

BASEMAPS = {
    "なし（QGISレイヤのみ）": None,
    "国土地理院・標準地図": {
        "type": "raster",
        "tiles": ["https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"],
        "attribution": "国土地理院",
        "tileSize": 256,
    },
    "国土地理院・全国最新写真": {
        "type": "raster",
        "tiles": ["https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg"],
        "attribution": "国土地理院",
        "tileSize": 256,
    },
    "国土地理院・淡色地図": {
        "type": "raster",
        "tiles": ["https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"],
        "attribution": "国土地理院",
        "tileSize": 256,
    },
    "OpenStreetMap": {
        "type": "raster",
        "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        "attribution": "© OpenStreetMap contributors",
        "tileSize": 256,
    },
}


# 出典（自動生成）用のラベル定義
#   ・国土地理院のベースマップ名 → 出典に載せる項目名
GSI_BASEMAP_LABEL = {
    "国土地理院・標準地図":     "標準地図",
    "国土地理院・淡色地図":     "淡色地図",
    "国土地理院・全国最新写真": "全国最新写真",
}
# 国土地理院の項目表示順（重複排除後にこの順へ正規化）
GSI_ITEM_ORDER = ["標準地図", "淡色地図", "全国最新写真", "標高タイル"]
# 標高タイルを使う微地形表現図のオプションキー（すべて「国土地理院/標高タイル」に集約）
MICRO_RELIEF_OPT_KEYS = ("csmap", "inyouzu", "mpirrim", "cimap", "colorrelief", "twi", "topex")


def build_data_attribution(basemap_name, basemap_name2, opts):
    """選択されたベースマップ・データレイヤから出典文字列を自動生成する。

    形式 :  '提供元/項目1、項目2 ｜ 提供元/項目'
      - 提供元と項目は「/」で区切る
      - 同一提供元内の複数項目は「、」で連結する
      - 提供元グループ同士は「 ｜ 」で連結する

    集約ルール:
      - 国土地理院 : 使用したベースマップ（標準地図/淡色地図/全国最新写真）に加え、
                     標高タイルを使う微地形表現図を使っていれば「標高タイル」を1つだけ追加
      - osm.org    : OpenStreetMap
      - Earth Search by Element 84 : Sentinel-2 API（衛星変化解析）
      - 気象庁     : 気象（ナウキャスト＋台風情報＝1セット）／キキクル＝1セット
      - Open-Meteo : weather API（気象オプションと同時に使用）
    """
    gsi = []          # 国土地理院の項目
    jma = []          # 気象庁の項目
    osm = False       # osm.org
    es84 = False      # Earth Search by Element 84
    openmeteo = False # Open-Meteo

    def _add(lst, item):
        if item not in lst:
            lst.append(item)

    # --- ベースマップ（種類1・種類2）---
    for bm in (basemap_name, basemap_name2):
        if not bm:
            continue
        if bm in GSI_BASEMAP_LABEL:
            _add(gsi, GSI_BASEMAP_LABEL[bm])
        elif bm == "OpenStreetMap":
            osm = True

    # --- 標高タイルを使う微地形表現図（すべて 国土地理院/標高タイル に集約）---
    if any(opts.get(k) for k in MICRO_RELIEF_OPT_KEYS):
        _add(gsi, "標高タイル")

    # --- 気象庁・Open-Meteo ---
    if opts.get("weather"):
        _add(jma, "ナウキャスト")
        _add(jma, "台風情報")
        openmeteo = True
    if opts.get("kikikuru"):
        _add(jma, "キキクル")

    # --- Sentinel-2（Earth Search by Element 84）---
    if opts.get("sentinel"):
        es84 = True

    # 提供元グループを既定順で組み立て
    groups = []
    if gsi:
        ordered = [x for x in GSI_ITEM_ORDER if x in gsi]
        groups.append("国土地理院/" + "、".join(ordered))
    if osm:
        groups.append("osm.org/OpenStreetMap")
    if es84:
        groups.append("Earth Search by Element 84/Sentinel-2 API")
    if jma:
        groups.append("気象庁/" + "、".join(jma))
    if openmeteo:
        groups.append("Open-Meteo/weather API")

    return " ｜ ".join(groups)


GEOM_MAP = {
    QgsWkbTypes.PointGeometry: "Point",
    QgsWkbTypes.LineGeometry: "LineString",
    QgsWkbTypes.PolygonGeometry: "Polygon",
}


# 単木アイコン（treesvg.js）が参照する属性フィールド名。
# データ側のフィールド名が異なる場合はここを変更する。
TREE_SVG_FIELDS = {
    "height": "樹高",        # 樹高（m）— アイコン・円の大小調整に使用
    "crown_ratio": "樹冠長率",  # 樹冠長率（1-100, 任意）— 形状3段階の切替に使用
    "species": "樹種",        # 樹種（スギ/ヒノキ/マツ類/カラマツ/その他N/その他L）— 色
}
# 樹高が Nodata の場合のフォールバック値（m）
TREE_SVG_DEFAULT_HEIGHT_M = 10.0
# 樹冠長率が Nodata／列が無い場合のフォールバック値（→ 中位"mid"形状になる）
TREE_SVG_DEFAULT_CROWN_RATIO = 40.0
# 2D黒円の半径 = 樹高 × この係数（m）。実寸でズーム連動表示する。
TREE_CIRCLE_RADIUS_PER_HEIGHT = 0.25

# 樹種 → アイコン名スラッグ（ASCII）。createTreeSVG の switch と一致させる。
# 末尾（その他L）が match のデフォルトを兼ねる。
TREE_SVG_SPECIES = [
    ("スギ", "sugi"),
    ("ヒノキ", "hinoki"),
    ("マツ類", "matsu"),
    ("カラマツ", "karamatsu"),
    ("その他N", "otherN"),
    ("その他L", "otherL"),
]
TREE_SVG_DEFAULT_SLUG = "otherL"
TREE_SVG_ICON_PREFIX = "treeicon_"

# 樹冠長率による形状3段階。
#   slug と「代表樹冠長率（アイコン生成に使う形状の値）」と「区間下限」を持つ。
#   区間: low = (-∞,30), mid = [30,50), high = [50,∞)
TREE_SVG_TIERS = [
    ("low", 20.0, None),   # 樹冠長率 < 30
    ("mid", 40.0, 30.0),   # 30 <= 樹冠長率 < 50
    ("high", 65.0, 50.0),  # 50 <= 樹冠長率
]
TREE_SVG_DEFAULT_TIER = "mid"

# このズーム未満では「樹冠のみ（樹幹を省略）」のアイコンに切り替える
TREE_SVG_TRUNK_MIN_ZOOM = 14
# アイコン基準の高さ（px）。icon-size はこの基準に対する倍率（=樹高ベース）になる。
TREE_SVG_ICON_BASE_PX = 768.0
# アイコンの基準幅（px）。treesvg.js の viewBox(100x160) と相似にする。
TREE_SVG_ICON_BASE_W_PX = TREE_SVG_ICON_BASE_PX * (100.0 / 160.0)
# icon-size の下限（基準px×この値 が最小表示サイズ）
TREE_SVG_ICON_MIN_SIZE = 0.04


# 単木SVGアイコン（treesvg.js）連携用 JS テンプレート。
_TREESVG_JS_TEMPLATE = r"""
// ========== 単木アイコン（treesvg.js / 有償オプション・WebGL symbol描画） ==========
(function setupTreeSvg(){
  const TREE_LAYERS = __TREE_LAYERS_JSON__;   // [{circle_id, symbol_id, source, source_layer}]
  const SPECIES     = __TREE_SPECIES_JSON__;  // [[樹種, slug], ...]
  const TIERS       = __TREE_TIERS_JSON__;    // [[tierSlug, 代表樹冠長率], ...]
  const ICON_PREFIX = __TREE_ICON_PREFIX__;
  const ICON_IMAGE_EXPR = __TREE_ICON_IMAGE_EXPR__;
  const ICON_SIZE_EXPR  = __TREE_ICON_SIZE_EXPR__;
  const ICON_W = __TREE_ICON_W_PX__;          // アイコン基準幅(px)
  const ICON_H = __TREE_ICON_H_PX__;          // アイコン基準高(px)
  const PR = Math.max(1, Math.min(3, window.devicePixelRatio || 1));

  let want3D = false;     // toggle3DView から渡される希望状態
  let treeReady = false;  // アイコン生成＆symbolレイヤ追加が完了したか

  function treeSvgAvailable(){ return (typeof createTreeSVG === 'function'); }

  // SVG文字列 → ImageData（指定px）にラスタライズ
  function svgToImageData(svgStr, wPx, hPx){
    return new Promise(function(resolve, reject){
      const img = new Image();
      img.onload = function(){
        const cw = Math.max(1, Math.round(wPx * PR));
        const ch = Math.max(1, Math.round(hPx * PR));
        const cv = document.createElement('canvas');
        cv.width = cw; cv.height = ch;
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cw, ch);
        ctx.drawImage(img, 0, 0, cw, ch);
        resolve(ctx.getImageData(0, 0, cw, ch));
      };
      img.onerror = reject;
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr);
    });
  }

  // 樹種ごとに「樹冠長率3段階の樹幹+樹冠」＋「樹冠のみ」を生成して登録
  async function buildIcons(){
    for (let i = 0; i < SPECIES.length; i++){
      const jp = SPECIES[i][0], slug = SPECIES[i][1];
      // 樹幹+樹冠（tier別）。全アイコン同一viewBox(=同一px)なので icon-size が統一できる
      for (let t = 0; t < TIERS.length; t++){
        const tierSlug = TIERS[t][0], rep = TIERS[t][1];
        const name = ICON_PREFIX + slug + '_' + tierSlug;
        if (map.hasImage(name)) continue;
        const svg = createTreeSVG({ 樹種: jp, 樹冠長率: rep, showTrunk: true });
        const data = await svgToImageData(svg, ICON_W, ICON_H);
        if (!map.hasImage(name)) map.addImage(name, data, { pixelRatio: PR });
      }
      // 樹冠のみ（低ズームLOD）
      const crownName = ICON_PREFIX + slug + '_crown';
      if (!map.hasImage(crownName)){
        const svg = createTreeSVG({ 樹種: jp, showTrunk: false });
        const data = await svgToImageData(svg, ICON_W, ICON_H);
        if (!map.hasImage(crownName)) map.addImage(crownName, data, { pixelRatio: PR });
      }
    }
  }

  function addSymbolLayers(){
    TREE_LAYERS.forEach(function(L){
      if (map.getLayer(L.symbol_id)) return;
      if (!map.getSource(L.source)) return;
      map.addLayer({
        id: L.symbol_id,
        type: 'symbol',
        source: L.source,
        'source-layer': L.source_layer,
        layout: {
          'icon-image': ICON_IMAGE_EXPR,
          'icon-size': ICON_SIZE_EXPR,
          'icon-anchor': 'bottom',
          'icon-allow-overlap': true,
          'icon-ignore-placement': true,
          'icon-pitch-alignment': 'viewport',   // 地形上に直立（ビルボード）
          'icon-rotation-alignment': 'viewport',
          'visibility': 'none'                   // 初期は非表示（2D = 円表示）
        }
      });
    });
  }

  function setCircleHidden(L, hidden){
    if (!map.getLayer(L.circle_id)) return;
    map.setPaintProperty(L.circle_id, 'circle-opacity', hidden ? 0 : 1);
    map.setPaintProperty(L.circle_id, 'circle-stroke-opacity', hidden ? 0 : 1);
  }
  function setSymbolVisible(L, show){
    if (!map.getLayer(L.symbol_id)) return;
    map.setLayoutProperty(L.symbol_id, 'visibility', show ? 'visible' : 'none');
  }

  // レイヤパネルでの ON/OFF を尊重するための判定。
  // チェックボックスは円レイヤ(circle_id)の layout 'visibility' を切り替えるが、
  // symbolレイヤ(symbol_id)は別管理でチェック対象外。そこで円レイヤの可視状態を
  // 「このレイヤが有効か」の基準として使う。
  function isLayerEnabled(L){
    if (!map.getLayer(L.circle_id)) return false;
    // 'visibility' 未設定時は undefined を返すため、その場合は既定の 'visible' とみなす
    return map.getLayoutProperty(L.circle_id, 'visibility') !== 'none';
  }

  // 現在の状態を反映: 3D かつ treeReady「かつレイヤパネルでONのとき」だけ樹木symbol。
  // パネルでチェックを外したレイヤは 3D でも円・symbol とも表示しない。
  function applyMode(){
    const active = want3D && treeReady && treeSvgAvailable();
    TREE_LAYERS.forEach(function(L){
      const on = active && isLayerEnabled(L);
      setCircleHidden(L, on);     // 樹木表示中のみ円を透明化
      setSymbolVisible(L, on);    // OFF のレイヤは 3D でも symbol を出さない
    });
  }

  async function initTreeSvg(){
    if (!treeSvgAvailable()){ treeReady = false; applyMode(); return; }
    try {
      await buildIcons();
      addSymbolLayers();
      treeReady = true;
    } catch (e){
      console.warn('[treesvg] アイコン生成に失敗しました。黒い円で表示を継続します。', e);
      treeReady = false;
    }
    applyMode();
  }

  // toggle3DView から呼ばれるフック（3D/2D 切替）
  window.__treeSvgOnViewModeChange = function(is3D){
    want3D = !!is3D;
    applyMode();
  };

  // レイヤパネルのチェック切替後に呼ばれ、現在の状態を再反映する。
  // （3D表示中にレイヤを ON/OFF したとき、円だけでなく symbol も追従させるため）
  window.__treeSvgReapply = function(){ applyMode(); };

  // 初期化は本体の map "load" ハンドラから呼ばれる
  // （保護JS treesvg.js の注入完了後・QGISレイヤのソース追加後に実行するため）。
  window.__treeSvgInit = function(){
    // 呼び出し時点では直前に追加したソースが読込中で map.loaded()=false のことがある。
    // かつ 'load' は再発火しないため、未ロード時は 'idle'（全読込完了）で確実に初期化する。
    if (map.loaded && map.loaded()) { initTreeSvg(); return; }
    map.once('idle', initTreeSvg);
  };
})();
"""


# ===== 属性検索（ベクタ fgb レイヤの属性値で地物検索）連携 JS テンプレート =====
_FEATURE_SEARCH_JS_TEMPLATE = r"""
// ========== 属性検索（ベクタレイヤの属性値で地物検索） ==========
(function setupFeatureSearch(){
  const FS_LAYERS = __FS_LAYERS__;
  const layerSel = document.getElementById("fs-layer");
  const fieldSel = document.getElementById("fs-field");
  const input    = document.getElementById("fs-input");
  const statusEl = document.getElementById("fs-status");
  if(!layerSel) return;

  // レイヤ選択プルダウンを現在の FS_LAYERS から組み直す。
  // （外部データ読込で実行時にレイヤが増減するため、その都度呼ぶ）
  function rebuildLayerOptions(){
    const prev = layerSel.value;
    layerSel.innerHTML = "";
    FS_LAYERS.forEach(function(l, i){
      const o = document.createElement("option");
      o.value = String(i); o.textContent = l.name;
      layerSel.appendChild(o);
    });
    // 1レイヤだけなら選択プルダウンは隠す
    layerSel.style.display = (FS_LAYERS.length <= 1) ? "none" : "";
    if(prev && Number(prev) < FS_LAYERS.length) layerSel.value = prev;
    refreshFields();
    // 検索対象が無いうちはバー全体を隠す（外部読込で増えたら表示）
    var bar = document.getElementById("feature-search-bar");
    if(bar) bar.style.display = FS_LAYERS.length ? "" : "none";
  }

  function refreshFields(){
    fieldSel.innerHTML = "";
    const l = FS_LAYERS[Number(layerSel.value) || 0];
    if(!l) return;
    (l.fields || []).forEach(function(f){
      const o = document.createElement("option");
      o.value = f; o.textContent = f;
      fieldSel.appendChild(o);
    });
  }
  layerSel.addEventListener("change", refreshFields);
  rebuildLayerOptions();
  input.addEventListener("keydown", function(e){ if(e.key === "Enter") runFeatureSearch(); });

  // 外部データ読込レイヤの実行時登録。importExternal から呼ばれる。
  //   layer = { id, name, fields:[...], features:[...] }（features はインメモリ全件）
  window.__featureSearchRegister = function(layer){
    if(!layer || !layer.id) return;
    for(var i=0;i<FS_LAYERS.length;i++){ if(FS_LAYERS[i].id === layer.id) return; } // 重複防止
    FS_LAYERS.push(layer);
    rebuildLayerOptions();
    // 検索バーが隠れている場合に備えて表示しておく
    var bar = document.getElementById("feature-search-bar");
    if(bar) bar.style.display = "";
  };
  window.__featureSearchUnregister = function(layerId){
    for(var i=FS_LAYERS.length-1;i>=0;i--){ if(FS_LAYERS[i].id === layerId) FS_LAYERS.splice(i,1); }
    rebuildLayerOptions();
  };

  function ensureHighlightLayers(){
    if(!map.getSource("_fsearch-src")){
      map.addSource("_fsearch-src", { type:"geojson", data:{ type:"FeatureCollection", features:[] }});
    }
    if(!map.getLayer("_fsearch-fill")){
      map.addLayer({ id:"_fsearch-fill", type:"fill", source:"_fsearch-src",
        filter:["==","$type","Polygon"],
        paint:{ "fill-color":"#ffd400", "fill-opacity":0.35 } });
    }
    if(!map.getLayer("_fsearch-line")){
      map.addLayer({ id:"_fsearch-line", type:"line", source:"_fsearch-src",
        filter:["in","$type","LineString","Polygon"],
        paint:{ "line-color":"#ff8c00", "line-width":3 } });
    }
    if(!map.getLayer("_fsearch-pt")){
      map.addLayer({ id:"_fsearch-pt", type:"circle", source:"_fsearch-src",
        filter:["==","$type","Point"],
        paint:{ "circle-color":"#ffd400", "circle-radius":8,
                "circle-stroke-color":"#ff8c00", "circle-stroke-width":3 } });
    }
  }

  window.clearFeatureSearch = function(){
    if(map.getSource("_fsearch-src"))
      map.getSource("_fsearch-src").setData({ type:"FeatureCollection", features:[] });
    if(input) input.value = "";
    if(statusEl) statusEl.textContent = "";
  };

  // レイヤごとに全件パース結果をキャッシュ（2回目以降の検索を高速化）
  const _fsCache = {};
  async function loadAllFeatures(l){
    // 外部データ読込レイヤ: 取り込み時に渡されたインメモリ全件を使う
    if(l.features && l.features.length !== undefined) return l.features;
    // マップのGeoJSONソースから直接読む（フォールバック）
    if(l.sourceId && map.getSource(l.sourceId)){
      const d = map.getSource(l.sourceId)._data;
      if(d && d.features) return d.features;
    }
    // fgb レイヤ: URL から全件デシリアライズ
    if(_fsCache[l.url]) return _fsCache[l.url];
    const resp = await fetch(l.url);
    if(!resp.ok) throw new Error("HTTP " + resp.status);
    const buf = new Uint8Array(await resp.arrayBuffer());
    // flatgeobuf.deserialize(bytes) は {type, features:[...]} を返す（全件・同期）
    const fc = flatgeobuf.deserialize(buf);
    const feats = (fc && fc.features) ? fc.features : [];
    _fsCache[l.url] = feats;
    return feats;
  }

  window.runFeatureSearch = async function(){
    const l = FS_LAYERS[Number(layerSel.value) || 0];
    const field = fieldSel.value;
    const kw = (input.value || "").trim();
    if(!l || !field || !kw){ return; }
    if(statusEl) statusEl.textContent = "検索中…";

    const matches = [];
    let minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity;
    function expand(g){
      function walk(c){
        if(typeof c[0] === "number"){
          if(c[0]<minX) minX=c[0]; if(c[1]<minY) minY=c[1];
          if(c[0]>maxX) maxX=c[0]; if(c[1]>maxY) maxY=c[1];
        } else { c.forEach(walk); }
      }
      if(g && g.coordinates) walk(g.coordinates);
    }

    let feats;
    try {
      feats = await loadAllFeatures(l);
    } catch(e){
      console.warn("[feature-search] load error:", e);
      if(statusEl) statusEl.textContent = "データ読込に失敗しました";
      return;
    }

    for(const f of feats){
      const v = f.properties ? f.properties[field] : undefined;
      if(v != null && String(v).indexOf(kw) !== -1){
        matches.push(f); expand(f.geometry);
      }
    }

    if(!matches.length){
      if(map.getSource("_fsearch-src"))
        map.getSource("_fsearch-src").setData({ type:"FeatureCollection", features:[] });
      if(statusEl) statusEl.textContent = "該当なし（" + feats.length + "件中）";
      return;
    }

    ensureHighlightLayers();
    map.getSource("_fsearch-src").setData({ type:"FeatureCollection", features: matches });

    if(isFinite(minX)){
      if(minX===maxX && minY===maxY){
        map.flyTo({ center:[minX, minY], zoom: Math.max(map.getZoom(), 16) });
      } else {
        map.fitBounds([[minX, minY],[maxX, maxY]], { padding: 60, maxZoom: 17 });
      }
    }
    if(statusEl) statusEl.textContent = matches.length + "件ヒット";
  };
})();
"""


class ForestGeoStudioDialog(QDialog, FORM_CLASS):


    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setupUi(self)

        self._layers = []
        self._styles = {}

        self.btnBrowse.clicked.connect(self._select_output)
        self.btnRun.clicked.connect(self._run_export)

        self.btnPointColor.clicked.connect(lambda: self._pick_color(self.txtPointColor))
        self.btnPointStroke.clicked.connect(lambda: self._pick_color(self.txtPointStroke))
        self.btnLineColor.clicked.connect(lambda: self._pick_color(self.txtLineColor))
        self.btnFillColor.clicked.connect(lambda: self._pick_color(self.txtFillColor))
        self.btnOutlineColor.clicked.connect(lambda: self._pick_color(self.txtOutlineColor))
        if hasattr(self, "btnLabelColor"):
            self.btnLabelColor.clicked.connect(lambda: self._pick_color(self.txtLabelColor))
        if hasattr(self, "btnLabelHaloColor"):
            self.btnLabelHaloColor.clicked.connect(lambda: self._pick_color(self.txtLabelHaloColor))

        # VectorTile スタイル用ボタン・コンボ
        if hasattr(self, "btnVtFillColor"):
            self.btnVtFillColor.clicked.connect(lambda: self._pick_color(self.txtVtFillColor))
        if hasattr(self, "btnVtOutlineColor"):
            self.btnVtOutlineColor.clicked.connect(lambda: self._pick_color(self.txtVtOutlineColor))
        if hasattr(self, "btnVtLineColor"):
            self.btnVtLineColor.clicked.connect(lambda: self._pick_color(self.txtVtLineColor))
        if hasattr(self, "btnVtPointColor"):
            self.btnVtPointColor.clicked.connect(lambda: self._pick_color(self.txtVtPointColor))
        if hasattr(self, "btnVtPointStroke"):
            self.btnVtPointStroke.clicked.connect(lambda: self._pick_color(self.txtVtPointStroke))
        if hasattr(self, "btnVtLabelColor"):
            self.btnVtLabelColor.clicked.connect(lambda: self._pick_color(self.txtVtLabelColor))
        if hasattr(self, "btnVtLabelHaloColor"):
            self.btnVtLabelHaloColor.clicked.connect(lambda: self._pick_color(self.txtVtLabelHaloColor))
        if hasattr(self, "cmbVtGeomType"):
            self.cmbVtGeomType.currentIndexChanged.connect(self._on_vt_geom_changed)

        # 属性値色分けルール
        if hasattr(self, "btnVtAddRule"):
            self.btnVtAddRule.clicked.connect(self._vt_add_rule_row)
        if hasattr(self, "btnVtRemoveRule"):
            self.btnVtRemoveRule.clicked.connect(self._vt_remove_rule_row)
        if hasattr(self, "tblVtColorRules"):
            self.tblVtColorRules.cellDoubleClicked.connect(self._vt_rule_cell_double_clicked)

        self.btnApplyStyle.clicked.connect(self._apply_style_to_layer)
        if hasattr(self, "btnSaveStyle"):
            self.btnSaveStyle.clicked.connect(self._save_style_to_file)
        if hasattr(self, "btnLoadStyle"):
            self.btnLoadStyle.clicked.connect(self._load_style_from_file)
        self.layerTable.itemSelectionChanged.connect(self._on_layer_selected)

        if hasattr(self, "cmbTheme"):
            self.cmbTheme.clear()
            self.cmbTheme.addItems(THEMES.keys())

        self._populate_layers()

    def _log(self, msg):
        print("[ForestGeoStudio]", msg)

    def _fetch_license_token(self, option_num, code):
        """
        ライセンスコードを認証サーバ(token.php)に送り、永続トークンを取得する。
        成功: トークン文字列 / 失敗・未入力: 空文字（→ 当該オプションは無効化）。
        """
        code = (code or "").strip()
        if not code:
            return ""
        import json as _json
        import urllib.request
        import urllib.error
        try:
            payload = _json.dumps(
                {"option": int(option_num), "license_code": code}
            ).encode("utf-8")
            req = urllib.request.Request(
                LICENSE_SERVER_BASE + "/token.php",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                obj = _json.loads(resp.read().decode("utf-8"))
                return obj.get("token", "") or ""
        except urllib.error.HTTPError as e:
            self._log(f"license option{option_num}: HTTP {e.code}")
            return ""
        except Exception as e:
            self._log(f"license option{option_num}: {e}")
            return ""

    def _legend_layers_top_to_bottom(self):
        layers = []
        root = QgsProject.instance().layerTreeRoot()

        def walk(node):
            for child in node.children():
                if isinstance(child, QgsLayerTreeLayer):
                    layer = child.layer()
                    if layer:
                        layers.append(layer)
                else:
                    walk(child)

        walk(root)
        known_ids = {layer.id() for layer in layers}
        for layer in QgsProject.instance().mapLayers().values():
            if layer.id() not in known_ids:
                layers.append(layer)
        return layers

    def _populate_layers(self):
        self.layerTable.setRowCount(0)
        self._layers = []
        self._styles = {}

        layers = self._legend_layers_top_to_bottom()
        self.layerTable.setColumnCount(5)
        self.layerTable.setHorizontalHeaderLabels(["出力", "表示", "レイヤ名", "種類", "ソース"])
        self.layerTable.horizontalHeader().setStretchLastSection(True)

        for layer in layers:
            row = self.layerTable.rowCount()
            self.layerTable.insertRow(row)
            self._layers.append(layer)

            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            chk = QCheckBox()
            chk.setChecked(True)
            chk_layout.addWidget(chk)
            self.layerTable.setCellWidget(row, 0, chk_widget)

            # 表示チェックボックス（出力対象だが初期非表示にする選択）
            vis_widget = QWidget()
            vis_layout = QHBoxLayout(vis_widget)
            vis_layout.setAlignment(Qt.AlignCenter)
            vis_layout.setContentsMargins(0, 0, 0, 0)
            vis_chk = QCheckBox()
            vis_chk.setChecked(True)  # デフォルトは表示
            vis_layout.addWidget(vis_chk)
            self.layerTable.setCellWidget(row, 1, vis_widget)

            self.layerTable.setItem(row, 2, QTableWidgetItem(layer.name()))
            self.layerTable.setItem(row, 3, QTableWidgetItem(self._layer_kind(layer)))

            src = layer.source()
            short_src = src if len(src) <= 60 else "..." + src[-57:]
            self.layerTable.setItem(row, 4, QTableWidgetItem(short_src))

            # レイヤ名（列2）のみ編集可能にする。他の列は誤編集防止のため編集不可。
            name_item = self.layerTable.item(row, 2)
            name_item.setFlags(name_item.flags() | Qt.ItemIsEditable)
            name_item.setToolTip("ダブルクリックで表示名を編集できます（WEB画面・凡例に反映されます）")
            for col in (3, 4):
                it = self.layerTable.item(row, col)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)

            self._styles[layer.id()] = self._default_style(layer)

        # レイヤ名セルのみダブルクリック等で編集できるようにする
        self.layerTable.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )

        self.layerTable.resizeColumnsToContents()
        if self.layerTable.rowCount():
            self.layerTable.selectRow(0)

    def _layer_kind(self, layer):
        if isinstance(layer, QgsVectorLayer):
            return GEOM_MAP.get(layer.geometryType(), "Vector")
        if QgsVectorTileLayer and isinstance(layer, QgsVectorTileLayer):
            return "VectorTile"
        if isinstance(layer, QgsRasterLayer):
            src = urllib.parse.unquote(layer.source())
            src_lower = src.lower()
            provider = layer.providerType().lower()
            if "pbf" in src_lower or "mvt" in src_lower or "vector" in src_lower:
                return "VectorTile"
            if "{z}" in src or "url=" in src or provider in ("wms", "xyz"):
                return "Raster(Tile)"
            return "Raster"
        return "Unknown"

    def _default_style(self, layer):
        kind = self._layer_kind(layer)
        if kind == "Point":
            return {
                "geom": "Point",
                "circle-color": "#e63946",
                "circle-radius": 8,
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": 1.5,
                "minzoom": 0,
                "maxzoom": 24,
                "label-enabled": False,
                "label-field": "",
                "text-size": 12,
                "text-color": "#222222",
                "text-halo-enabled": True,
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5,
                "text-minzoom": 0,
                "text-maxzoom": 24,
                "vt-color-rule-enabled": False,
                "vt-color-rule-field": "",
                "vt-color-rules": [],
            }
        if kind == "LineString":
            return {
                "geom": "LineString",
                "line-color": "#1d6fa4",
                "line-width": 2.0,
                "line-opacity": 1.0,
                "minzoom": 0,
                "maxzoom": 24,
                "label-enabled": False,
                "label-field": "",
                "text-size": 12,
                "text-color": "#222222",
                "text-halo-enabled": True,
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5,
                "text-minzoom": 0,
                "text-maxzoom": 24,
                "vt-color-rule-enabled": False,
                "vt-color-rule-field": "",
                "vt-color-rules": [],
            }
        if kind == "Polygon":
            return {
                "geom": "Polygon",
                "fill-color": "#2d8a4e",
                "fill-opacity": 0.5,
                "fill-outline-color": "#ffffff",
                "line-opacity": 1.0,
                "minzoom": 0,
                "maxzoom": 24,
                "label-enabled": False,
                "label-field": "",
                "text-size": 12,
                "text-color": "#222222",
                "text-halo-enabled": True,
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5,
                "text-minzoom": 0,
                "text-maxzoom": 24,
                "vt-color-rule-enabled": False,
                "vt-color-rule-field": "",
                "vt-color-rules": [],
            }
        if kind == "VectorTile":
            return {
                "geom": "VectorTile",
                "tile_url": self._extract_tile_url(layer.source()) or "",
                # ソースレイヤ・ジオメトリ種別
                "vt-source": "",
                "vt-source-layer": "",
                "vt-geom-type": "Polygon",   # Polygon / LineString / Point
                # Polygon
                "fill-color": "#2d8a4e",
                "fill-opacity": 0.6,
                "vt-outline-color": "#ffffff",
                "vt-outline-width": 1.0,
                # LineString
                "vt-line-color": "#1d6fa4",
                "vt-line-width": 2.0,
                "vt-line-opacity": 1.0,
                # Point
                "vt-circle-color": "#e63946",
                "vt-circle-radius": 6,
                "vt-circle-stroke": "#ffffff",
                # 単木SVGアイコン（有償オプション treesvg.js）
                "vt-tree-svg-enabled": False,
                # Label
                "vt-label-enabled": False,
                "vt-label-field": "",
                "vt-label-size": 12,
                "vt-label-color": "#222222",
                "vt-label-halo": True,
                "vt-label-halo-color": "#ffffff",
                "vt-label-minzoom": 0,
                "vt-label-maxzoom": 24,
                # 属性値色分け
                "vt-color-rule-enabled": False,
                "vt-color-rule-field": "",
                "vt-color-rules": [],  # [{"value": "...", "color": "#xxxxxx"}, ...]
            }
        if kind in ("Raster", "Raster(Tile)"):
            return {
                "geom": kind,
                "raster-opacity": 1.0,
                "minzoom": 0,
                "maxzoom": 24,
            }
        return {"geom": kind}

    def _on_layer_selected(self):
        row = self.layerTable.currentRow()
        if row < 0 or row >= len(self._layers):
            return

        layer = self._layers[row]
        style = self._styles.get(layer.id(), {})
        geom = style.get("geom", "")

        self.widgetPoint.setVisible(geom == "Point")
        self.widgetLine.setVisible(geom == "LineString")
        self.widgetPolygon.setVisible(geom == "Polygon")
        self.widgetVectorTile.setVisible(geom == "VectorTile")
        if hasattr(self, "widgetRaster"):
            is_raster = geom in ("Raster", "Raster(Tile)")
            self.widgetRaster.setVisible(is_raster)
            if is_raster:
                self.spinRasterOpacity.setValue(float(style.get("raster-opacity", 1.0)))
                self.spinRasterMinZoom.setValue(float(style.get("minzoom", 0)))
                self.spinRasterMaxZoom.setValue(float(style.get("maxzoom", 24)))

        # 属性値色分けUIはfgbベクタでも表示
        if hasattr(self, "widgetVtColorRules"):
            show_color_rules = geom in ("Point", "LineString", "Polygon", "VectorTile")
            self.widgetVtColorRules.setVisible(show_color_rules)
            if geom in ("Point", "LineString", "Polygon") and show_color_rules:
                self.chkVtColorRule.setChecked(bool(style.get("vt-color-rule-enabled", False)))
                self.txtVtColorRuleField.setText(style.get("vt-color-rule-field", ""))
                self._vt_load_rules(style.get("vt-color-rules", []))
        if hasattr(self, "widgetLabel"):
            self.widgetLabel.setVisible(geom in ("Point", "LineString", "Polygon"))
            if geom in ("Point", "LineString", "Polygon"):
                current_field = style.get("label-field", "")
                self.cmbLabelField.blockSignals(True)
                self.cmbLabelField.clear()
                self.cmbLabelField.addItem("")
                for field in layer.fields():
                    self.cmbLabelField.addItem(field.name())
                index = self.cmbLabelField.findText(current_field)
                self.cmbLabelField.setCurrentIndex(index if index >= 0 else 0)
                self.cmbLabelField.blockSignals(False)
                self.chkLabelEnabled.setChecked(bool(style.get("label-enabled", False)))
                self.spinLabelSize.setValue(int(style.get("text-size", 12)))
                self.txtLabelColor.setText(style.get("text-color", "#222222"))
                self.chkLabelHalo.setChecked(bool(style.get("text-halo-enabled", True)))
                self.txtLabelHaloColor.setText(style.get("text-halo-color", "#ffffff"))
                self.spinLabelHaloWidth.setValue(float(style.get("text-halo-width", 1.5)))
                self.spinLabelMinZoom.setValue(float(style.get("text-minzoom", 0)))
                self.spinLabelMaxZoom.setValue(float(style.get("text-maxzoom", 24)))

        if geom == "Point":
            self.txtPointColor.setText(style.get("circle-color", "#e63946"))
            self.spinPointSize.setValue(int(style.get("circle-radius", 8)))
            self.txtPointStroke.setText(style.get("circle-stroke-color", "#ffffff"))
            if hasattr(self, "spinPointMinZoom"):
                self.spinPointMinZoom.setValue(float(style.get("minzoom", 0)))
                self.spinPointMaxZoom.setValue(float(style.get("maxzoom", 24)))
        elif geom == "LineString":
            self.txtLineColor.setText(style.get("line-color", "#1d6fa4"))
            self.spinLineWidth.setValue(float(style.get("line-width", 2.0)))
            self.spinLineOpacity.setValue(float(style.get("line-opacity", 1.0)))
            if hasattr(self, "spinLineMinZoom"):
                self.spinLineMinZoom.setValue(float(style.get("minzoom", 0)))
                self.spinLineMaxZoom.setValue(float(style.get("maxzoom", 24)))
        elif geom == "Polygon":
            self.txtFillColor.setText(style.get("fill-color", "#2d8a4e"))
            self.spinFillOpacity.setValue(float(style.get("fill-opacity", 0.5)))
            self.txtOutlineColor.setText(style.get("fill-outline-color", "#ffffff"))
            if hasattr(self, "spinOutlineOpacity"):
                self.spinOutlineOpacity.setValue(float(style.get("line-opacity", 1.0)))
            if hasattr(self, "spinPolygonMinZoom"):
                self.spinPolygonMinZoom.setValue(float(style.get("minzoom", 0)))
                self.spinPolygonMaxZoom.setValue(float(style.get("maxzoom", 24)))
        elif geom == "VectorTile":
            self.txtVtUrl.setText(style.get("tile_url", ""))
            self.txtVtSource.setText(style.get("vt-source", ""))
            self.txtVtSourceLayer.setText(style.get("vt-source-layer", ""))
            geom_type = style.get("vt-geom-type", "Polygon")
            geom_map = {"Polygon": 0, "LineString": 1, "Point": 2}
            self.cmbVtGeomType.setCurrentIndex(geom_map.get(geom_type, 0))
            self._update_vt_geom_widgets(geom_type)
            # Polygon
            self.txtVtFillColor.setText(style.get("fill-color", "#2d8a4e"))
            self.spinVtFillOpacity.setValue(float(style.get("fill-opacity", 0.6)))
            self.txtVtOutlineColor.setText(style.get("vt-outline-color", "#ffffff"))
            self.spinVtOutlineWidth.setValue(float(style.get("vt-outline-width", 1.0)))
            # LineString
            self.txtVtLineColor.setText(style.get("vt-line-color", "#1d6fa4"))
            self.spinVtLineWidth.setValue(float(style.get("vt-line-width", 2.0)))
            self.spinVtLineOpacity.setValue(float(style.get("vt-line-opacity", 1.0)))
            # Point
            self.txtVtPointColor.setText(style.get("vt-circle-color", "#e63946"))
            self.spinVtPointRadius.setValue(int(style.get("vt-circle-radius", 6)))
            self.txtVtPointStroke.setText(style.get("vt-circle-stroke", "#ffffff"))
            # 単木SVGアイコン（有償オプション treesvg.js）
            if hasattr(self, "chkVtTreeSvg"):
                self.chkVtTreeSvg.setChecked(bool(style.get("vt-tree-svg-enabled", False)))
            # Label
            self.chkVtLabelEnabled.setChecked(bool(style.get("vt-label-enabled", False)))
            self.txtVtLabelField.setText(style.get("vt-label-field", ""))
            self.spinVtLabelSize.setValue(int(style.get("vt-label-size", 12)))
            self.txtVtLabelColor.setText(style.get("vt-label-color", "#222222"))
            self.chkVtLabelHalo.setChecked(bool(style.get("vt-label-halo", True)))
            self.txtVtLabelHaloColor.setText(style.get("vt-label-halo-color", "#ffffff"))
            if hasattr(self, "spinVtLabelMinZoom"):
                self.spinVtLabelMinZoom.setValue(float(style.get("vt-label-minzoom", 0)))
                self.spinVtLabelMaxZoom.setValue(float(style.get("vt-label-maxzoom", 24)))
            # 属性値色分け
            if hasattr(self, "chkVtColorRule"):
                self.chkVtColorRule.setChecked(bool(style.get("vt-color-rule-enabled", False)))
                self.txtVtColorRuleField.setText(style.get("vt-color-rule-field", ""))
                self._vt_load_rules(style.get("vt-color-rules", []))

    def _update_vt_geom_widgets(self, geom_type):
        #ジオメトリ種別に応じてポリゴン/ライン/ポイントの各UIを切り替え
        self.widgetVtPolygon.setVisible(geom_type == "Polygon")
        self.widgetVtLine.setVisible(geom_type == "LineString")
        self.widgetVtPoint.setVisible(geom_type == "Point")
        # 単木SVGアイコンのUIはPointのときのみ表示
        if hasattr(self, "widgetVtTreeSvg"):
            self.widgetVtTreeSvg.setVisible(geom_type == "Point")

    def _on_vt_geom_changed(self, index):
        labels = ["Polygon", "LineString", "Point"]
        geom_type = labels[index] if 0 <= index < len(labels) else "Polygon"
        self._update_vt_geom_widgets(geom_type)

    # 属性値色分け UI ヘルパー
    def _vt_load_rules(self, rules):
        #スタイルのルールリストをtblVtColorRulesに展開
        tbl = self.tblVtColorRules
        tbl.setRowCount(0)
        for rule in rules:
            row = tbl.rowCount()
            tbl.insertRow(row)
            # 列0: 属性値（文字列）
            tbl.setItem(row, 0, QTableWidgetItem(str(rule.get("value", ""))))
            # 列1: 数値下限（空文字なら文字列モード）
            lo = rule.get("num_min", "")
            tbl.setItem(row, 1, QTableWidgetItem("" if lo == "" else str(lo)))
            # 列2: 数値上限
            hi = rule.get("num_max", "")
            tbl.setItem(row, 2, QTableWidgetItem("" if hi == "" else str(hi)))
            # 列3: 色
            color = rule.get("color", "#cccccc")
            item = QTableWidgetItem(color)
            item.setBackground(QBrush(QColor(color)))
            item.setForeground(QBrush(QColor("#000000" if QColor(color).lightness() > 128 else "#ffffff")))
            tbl.setItem(row, 3, item)
            # 列4: 不透明度（空欄なら上段の既定値を継承）
            op = rule.get("opacity", "")
            tbl.setItem(row, 4, QTableWidgetItem("" if op == "" else str(op)))
            # 列5: 枠幅／ライン幅（空欄なら上段の既定値を継承）
            wd = rule.get("width", "")
            tbl.setItem(row, 5, QTableWidgetItem("" if wd == "" else str(wd)))

    def _vt_collect_rules(self):
        #tblVtColorRulesからルールリストを収集して返す
        tbl = self.tblVtColorRules
        rules = []
        for row in range(tbl.rowCount()):
            val_item = tbl.item(row, 0)
            lo_item  = tbl.item(row, 1)
            hi_item  = tbl.item(row, 2)
            col_item = tbl.item(row, 3)
            op_item  = tbl.item(row, 4)
            wd_item  = tbl.item(row, 5)
            # 不透明度・枠幅は任意。空欄なら未指定（上段の既定値を継承）。
            op_text = op_item.text().strip() if op_item else ""
            wd_text = wd_item.text().strip() if wd_item else ""

            def _apply_extras(rule):
                if op_text != "":
                    try:
                        rule["opacity"] = float(op_text)
                    except ValueError:
                        pass
                if wd_text != "":
                    try:
                        rule["width"] = float(wd_text)
                    except ValueError:
                        pass
                return rule

            if col_item:
                color = col_item.text().strip()
                lo_text = lo_item.text().strip() if lo_item else ""
                hi_text = hi_item.text().strip() if hi_item else ""
                val_text = val_item.text().strip() if val_item else ""
                # 数値列が両方入力されていれば数値ルール、そうでなければ文字列ルール
                if lo_text != "" or hi_text != "":
                    rule = {"value": "", "color": color}
                    if lo_text != "":
                        try:
                            rule["num_min"] = float(lo_text)
                        except ValueError:
                            rule["num_min"] = lo_text
                    if hi_text != "":
                        try:
                            rule["num_max"] = float(hi_text)
                        except ValueError:
                            rule["num_max"] = hi_text
                    if color:
                        rules.append(_apply_extras(rule))
                elif color:
                    # val_text が空文字列("")のときも有効な条件として扱う（元データが空欄の地物に色付け可能）
                    rules.append(_apply_extras({"value": val_text, "color": color}))
        return rules

    def _vt_add_rule_row(self):
        #行追加ボタン
        tbl = self.tblVtColorRules
        row = tbl.rowCount()
        tbl.insertRow(row)
        tbl.setItem(row, 0, QTableWidgetItem(""))   # 文字列
        tbl.setItem(row, 1, QTableWidgetItem(""))   # 数値下限
        tbl.setItem(row, 2, QTableWidgetItem(""))   # 数値上限
        item = QTableWidgetItem("#cccccc")
        item.setBackground(QBrush(QColor("#cccccc")))
        tbl.setItem(row, 3, item)                   # 色
        tbl.setItem(row, 4, QTableWidgetItem(""))   # 不透明度（空欄=既定）
        tbl.setItem(row, 5, QTableWidgetItem(""))   # 枠幅／ライン幅（空欄=既定）

    def _vt_remove_rule_row(self):
        #行削除ボタン
        tbl = self.tblVtColorRules
        rows = sorted(set(idx.row() for idx in tbl.selectedIndexes()), reverse=True)
        for row in rows:
            tbl.removeRow(row)
        if not rows and tbl.rowCount() > 0:
            tbl.removeRow(tbl.rowCount() - 1)

    def _vt_rule_cell_double_clicked(self, row, col):
        #カラム3（色）をダブルクリックしたらカラーダイアログを開く
        if col != 3:
            return
        tbl = self.tblVtColorRules
        item = tbl.item(row, col)
        current = QColor(item.text() if item else "#cccccc")
        color = QColorDialog.getColor(current, self, "色を選択")
        if color.isValid():
            if not item:
                item = QTableWidgetItem()
                tbl.setItem(row, col, item)
            item.setText(color.name())
            item.setBackground(QBrush(color))
            item.setForeground(QBrush(QColor("#000000" if color.lightness() > 128 else "#ffffff")))

    def _apply_style_to_layer(self):
        row = self.layerTable.currentRow()
        if row < 0 or row >= len(self._layers):
            return

        layer = self._layers[row]
        style = self._styles.get(layer.id(), {})
        geom = style.get("geom", "")

        if geom == "Point":
            style["circle-color"] = self.txtPointColor.text().strip()
            style["circle-radius"] = self.spinPointSize.value()
            style["circle-stroke-color"] = self.txtPointStroke.text().strip()
            if hasattr(self, "spinPointMinZoom"):
                style["minzoom"] = self.spinPointMinZoom.value()
                style["maxzoom"] = self.spinPointMaxZoom.value()
        elif geom == "LineString":
            style["line-color"] = self.txtLineColor.text().strip()
            style["line-width"] = self.spinLineWidth.value()
            style["line-opacity"] = self.spinLineOpacity.value()
            if hasattr(self, "spinLineMinZoom"):
                style["minzoom"] = self.spinLineMinZoom.value()
                style["maxzoom"] = self.spinLineMaxZoom.value()
        elif geom == "Polygon":
            style["fill-color"] = self.txtFillColor.text().strip()
            style["fill-opacity"] = self.spinFillOpacity.value()
            style["fill-outline-color"] = self.txtOutlineColor.text().strip()
            if hasattr(self, "spinOutlineOpacity"):
                style["line-opacity"] = self.spinOutlineOpacity.value()
            if hasattr(self, "spinPolygonMinZoom"):
                style["minzoom"] = self.spinPolygonMinZoom.value()
                style["maxzoom"] = self.spinPolygonMaxZoom.value()
        if geom in ("Point", "LineString", "Polygon") and hasattr(self, "widgetLabel"):
            style["label-enabled"] = self.chkLabelEnabled.isChecked()
            style["label-field"] = self.cmbLabelField.currentText().strip()
            style["text-size"] = self.spinLabelSize.value()
            style["text-color"] = self.txtLabelColor.text().strip()
            style["text-halo-enabled"] = self.chkLabelHalo.isChecked()
            style["text-halo-color"] = self.txtLabelHaloColor.text().strip()
            style["text-halo-width"] = self.spinLabelHaloWidth.value()
            style["text-minzoom"] = self.spinLabelMinZoom.value()
            style["text-maxzoom"] = self.spinLabelMaxZoom.value()
            # 属性値色分け（fgb共通）
            if hasattr(self, "chkVtColorRule"):
                style["vt-color-rule-enabled"] = self.chkVtColorRule.isChecked()
                style["vt-color-rule-field"] = self.txtVtColorRuleField.text().strip()
                style["vt-color-rules"] = self._vt_collect_rules()

        elif geom == "VectorTile":
            style["tile_url"] = self.txtVtUrl.text().strip()
            style["vt-source"] = self.txtVtSource.text().strip()
            style["vt-source-layer"] = self.txtVtSourceLayer.text().strip()
            geom_type_labels = ["Polygon", "LineString", "Point"]
            style["vt-geom-type"] = geom_type_labels[self.cmbVtGeomType.currentIndex()]
            style["fill-color"] = self.txtVtFillColor.text().strip()
            style["fill-opacity"] = self.spinVtFillOpacity.value()
            style["vt-outline-color"] = self.txtVtOutlineColor.text().strip()
            style["vt-outline-width"] = self.spinVtOutlineWidth.value()
            style["vt-line-color"] = self.txtVtLineColor.text().strip()
            style["vt-line-width"] = self.spinVtLineWidth.value()
            style["vt-line-opacity"] = self.spinVtLineOpacity.value()
            style["vt-circle-color"] = self.txtVtPointColor.text().strip()
            style["vt-circle-radius"] = self.spinVtPointRadius.value()
            style["vt-circle-stroke"] = self.txtVtPointStroke.text().strip()
            if hasattr(self, "chkVtTreeSvg"):
                style["vt-tree-svg-enabled"] = self.chkVtTreeSvg.isChecked()
            style["vt-label-enabled"] = self.chkVtLabelEnabled.isChecked()
            style["vt-label-field"] = self.txtVtLabelField.text().strip()
            style["vt-label-size"] = self.spinVtLabelSize.value()
            style["vt-label-color"] = self.txtVtLabelColor.text().strip()
            style["vt-label-halo"] = self.chkVtLabelHalo.isChecked()
            style["vt-label-halo-color"] = self.txtVtLabelHaloColor.text().strip()
            if hasattr(self, "spinVtLabelMinZoom"):
                style["vt-label-minzoom"] = self.spinVtLabelMinZoom.value()
                style["vt-label-maxzoom"] = self.spinVtLabelMaxZoom.value()
            # 属性値色分けルール
            if hasattr(self, "chkVtColorRule"):
                style["vt-color-rule-enabled"] = self.chkVtColorRule.isChecked()
                style["vt-color-rule-field"] = self.txtVtColorRuleField.text().strip()
                style["vt-color-rules"] = self._vt_collect_rules()

        elif geom in ("Raster", "Raster(Tile)") and hasattr(self, "spinRasterOpacity"):
            style["raster-opacity"] = self.spinRasterOpacity.value()
            style["minzoom"] = self.spinRasterMinZoom.value()
            style["maxzoom"] = self.spinRasterMaxZoom.value()

        self._styles[layer.id()] = style
        self._log(f"Style applied: {layer.name()} -> {style}")

    # ===================== レイヤ単位のスタイル設定 保存/読込 =====================
    # 1レイヤ分のスタイル設定（self._styles[layer.id()] の dict）をファイルに保存。
    STYLE_FILE_FORMAT = "forestgeostudio-layer-style"
    STYLE_FILE_VERSION = 1

    def _save_style_to_file(self):
        row = self.layerTable.currentRow()
        if row < 0 or row >= len(self._layers):
            QMessageBox.information(
                self, "スタイル保存",
                "保存するレイヤを一覧から選択してください。"
            )
            return

        layer = self._layers[row]

        # UIに入力されている最新の内容をスタイルへ反映してから保存する。
        # （「適用」を押し忘れていても、画面に見えている設定がそのまま保存される）
        self._apply_style_to_layer()
        style = self._styles.get(layer.id(), {})
        if not style:
            QMessageBox.information(
                self, "スタイル保存",
                "このレイヤには保存できるスタイル設定がありません。"
            )
            return

        default_base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", layer.name()).strip().rstrip(". ")
        if not default_base:
            default_base = "layer"
        default_name = default_base + ".fgstyle"
        path, _ = QFileDialog.getSaveFileName(
            self, "このレイヤのスタイル設定を保存",
            default_name,
            "スタイル設定ファイル (*.fgstyle);;JSONファイル (*.json);;すべてのファイル (*)"
        )
        if not path:
            return
        # 拡張子が付いていなければ .fgstyle を補う
        if not os.path.splitext(path)[1]:
            path += ".fgstyle"

        payload = {
            "_format": self.STYLE_FILE_FORMAT,
            "_version": self.STYLE_FILE_VERSION,
            "_layer_name": layer.name(),
            "geom": style.get("geom", ""),
            "style": style,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log("Style save error: " + traceback.format_exc())
            QMessageBox.warning(
                self, "スタイル保存",
                "スタイル設定の保存に失敗しました。\n" + str(e)
            )
            return

        self._log(f"Style saved: {layer.name()} -> {path}")
        QMessageBox.information(
            self, "スタイル保存",
            "このレイヤのスタイル設定を保存しました。\n" + path
        )

    def _load_style_from_file(self):
        row = self.layerTable.currentRow()
        if row < 0 or row >= len(self._layers):
            QMessageBox.information(
                self, "スタイル読込",
                "読み込み先のレイヤを一覧から選択してください。"
            )
            return

        layer = self._layers[row]

        path, _ = QFileDialog.getOpenFileName(
            self, "スタイル設定ファイルを読み込み",
            "",
            "スタイル設定ファイル (*.fgstyle *.json);;すべてのファイル (*)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            self._log("Style load error: " + traceback.format_exc())
            QMessageBox.warning(
                self, "スタイル読込",
                "スタイル設定ファイルの読み込みに失敗しました。\n" + str(e)
            )
            return

        # 保存形式の検証。本プラグインが保存したラッパー形式なら "style" を取り出す。
        if isinstance(payload, dict) and isinstance(payload.get("style"), dict):
            saved_style = payload["style"]
        elif isinstance(payload, dict) and payload.get("geom"):
            saved_style = payload
        else:
            QMessageBox.warning(
                self, "スタイル読込",
                "このファイルは有効なスタイル設定ではないようです。"
            )
            return

        current_style = self._styles.get(layer.id(), {})
        current_geom = current_style.get("geom", "")
        saved_geom = saved_style.get("geom", "")

        # ジオメトリ種別が異なる場合は警告し、続行可否をユーザに確認する。
        if saved_geom and current_geom and saved_geom != current_geom:
            ret = QMessageBox.question(
                self, "スタイル読込",
                "選択中レイヤの種別（{cur}）と、ファイルのスタイル種別（{saved}）が異なります。\n"
                "種別が一致する項目のみがプリセットされます。続行しますか？".format(
                    cur=current_geom, saved=saved_geom
                ),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

        # プリセット適用：保存値で上書きしつつ、レイヤ固有の識別情報は保持する。
        new_style = dict(current_style)
        new_style.update(saved_style)
        if current_geom:
            new_style["geom"] = current_geom
        if "tile_url" in current_style:
            new_style["tile_url"] = current_style.get("tile_url", "")

        self._styles[layer.id()] = new_style
        # UIへ反映（プリセット）。この後ユーザが手修正し「適用」を押せば確定する。
        self._on_layer_selected()

        self._log(f"Style preset loaded: {layer.name()} <- {path}")
        QMessageBox.information(
            self, "スタイル読込",
            "スタイル設定を初期値としてプリセットしました。\n"
            "必要に応じて各項目を手修正してから「このレイヤにスタイルを適用」を押してください。"
        )

    def _pick_color(self, line_edit):
        current = QColor(line_edit.text())
        color = QColorDialog.getColor(current, self, "色を選択")
        if color.isValid():
            line_edit.setText(color.name())
            line_edit.setStyleSheet(
                f"background-color:{color.name()};"
                f"color:{'#000' if color.lightness() > 128 else '#fff'};"
            )

    def _select_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "HTML保存先", "", "HTML (*.html)")
        if path:
            if not path.lower().endswith(".html"):
                path += ".html"
            self.txtOutput.setText(path)

    def _safe_filename(self, name, used_names):
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
        safe = safe.rstrip(". ")
        if not safe:
            safe = "layer"

        candidate = f"{safe}.fgb"
        index = 2
        while candidate.lower() in used_names:
            candidate = f"{safe}_{index}.fgb"
            index += 1
        used_names.add(candidate.lower())
        return candidate

    def _safe_id(self, name, used_ids):
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_")
        if not safe:
            safe = "layer"
        candidate = safe
        index = 2
        while candidate in used_ids:
            candidate = f"{safe}_{index}"
            index += 1
        used_ids.add(candidate)
        return candidate

    def _extract_tile_url(self, source):
        src = urllib.parse.unquote(source or "")
        match = re.search(r"url=([^&]+)", src)
        if match:
            return match.group(1)
        if "{z}" in src and "{x}" in src and "{y}" in src:
            return src
        match = re.search(r"(https?://[^|&\s]+)", src)
        if match:
            return match.group(1)
        return None

    def _display_name(self, row, layer):
        """レイヤテーブル列2で編集された表示名を返す（未編集ならQGISのレイヤ名）。"""
        item = self.layerTable.item(row, 2)
        if item:
            text = item.text().strip()
            if text:
                return text
        return layer.name()

    def _checked_layer_rows(self):
        rows = []
        for row, layer in enumerate(self._layers):
            chk_widget = self.layerTable.cellWidget(row, 0)
            chk = chk_widget.findChild(QCheckBox) if chk_widget else None
            if chk and chk.isChecked():
                vis_widget = self.layerTable.cellWidget(row, 1)
                vis_chk = vis_widget.findChild(QCheckBox) if vis_widget else None
                initial_visible = vis_chk.isChecked() if vis_chk else True
                rows.append((row, layer, initial_visible))
        return rows

    def _run_export(self):
        try:
            self._apply_style_to_layer()

            output_html = self.txtOutput.text().strip()
            if not output_html:
                QMessageBox.warning(self, "エラー", "出力先 HTML ファイルを指定してください。")
                return

            out_dir = os.path.dirname(output_html)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir)

            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            export_layers = []
            used_filenames = set()
            used_ids = set()

            for row, layer, initial_visible in reversed(self._checked_layer_rows()):
                disp_name = self._display_name(row, layer)
                layer_id = self._safe_id(disp_name, used_ids)
                style = self._styles.get(layer.id(), {})
                kind = self._layer_kind(layer)

                if isinstance(layer, QgsVectorLayer):
                    file_name = self._safe_filename(disp_name, used_filenames)
                    path = os.path.join(out_dir, file_name)

                    transform = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                    geom_type = GEOM_MAP.get(layer.geometryType(), "Point")

                    # 大容量データ対応: QgsVectorFileWriter 低レベルAPI でストリーミング書き込み。
                    opts = QgsVectorFileWriter.SaveVectorOptions()
                    opts.driverName = "FlatGeobuf"
                    opts.fileEncoding = "UTF-8"

                    # QgsVectorFileWriter コンストラクタで直接ファイルを開き、
                    # addFeature() で1件ずつストリーミング書き込みする。
                    writer = QgsVectorFileWriter(
                        path,
                        "UTF-8",
                        layer.fields(),
                        layer.wkbType(),
                        dest_crs,
                        "FlatGeobuf",
                    )
                    if writer.hasError():
                        self._log(f"FlatGeobuf create error: {layer.name()} -> {writer.errorMessage()}")
                    else:
                        for feature in layer.getFeatures():
                            geom = feature.geometry()
                            if not geom or geom.isNull():
                                continue
                            out_feat = QgsFeature(layer.fields())
                            out_geom = feature.geometry()
                            out_geom.transform(transform)
                            out_feat.setGeometry(out_geom)
                            out_feat.setAttributes(feature.attributes())
                            writer.addFeature(out_feat)
                    del writer  # デストラクタでflush・クローズ

                    export_layers.append({
                        "kind": "fgb",
                        "id": layer_id,
                        "name": disp_name,
                        "file": file_name,
                        "url": urllib.parse.quote(file_name),
                        "geom": geom_type,
                        "style": style,
                        "initial_visible": initial_visible,
                        "fields": [f.name() for f in layer.fields()],
                    })

                elif (QgsVectorTileLayer and isinstance(layer, QgsVectorTileLayer)) or isinstance(layer, QgsRasterLayer):
                    url = self._extract_tile_url(layer.source())
                    if kind == "VectorTile":
                        url = style.get("tile_url") or url
                    if not url:
                        self._log(f"Skipping raster/vector tile without URL: {layer.name()}")
                        continue

                    if kind == "VectorTile":
                        export_layers.append({
                            "kind": "vector-tile",
                            "id": layer_id,
                            "name": disp_name,
                            "url": url,
                            "source": style.get("vt-source") or layer_id,
                            "style": style,
                            "initial_visible": initial_visible,
                        })
                    else:
                        export_layers.append({
                            "kind": "raster",
                            "id": layer_id,
                            "name": disp_name,
                            "url": url,
                            "style": style,
                            "initial_visible": initial_visible,
                        })

            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            src_crs = canvas.mapSettings().destinationCrs()
            xfm = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
            extent = xfm.transformBoundingBox(extent)

            title = self.txtTitle.text().strip() or "WEB-GIS"
            source = self.txtSource.text().strip()
            basemap_name = self.cmbBasemap.currentText()
            basemap_name2 = self.cmbBasemap2.currentText() if hasattr(self, "cmbBasemap2") else "なし（QGISレイヤのみ）"

            opts_ui = {
                "layer_panel": self.chkLayerPanel.isChecked(),
                "zoom_ctrl": self.chkZoom.isChecked(),
                "scale": self.chkScale.isChecked(),
                "coord": self.chkCoord.isChecked(),
                "addr_search": self.chkAddrSearch.isChecked(),
                "gps": self.chkGps.isChecked(),
                "share_link": self.chkShareLink.isChecked() if hasattr(self, "chkShareLink") else True,
                "popup": self.chkPopup.isChecked(),
                "area_calc": self.chkAreaCalc.isChecked() if hasattr(self, "chkAreaCalc") else False,
                "dist_calc": self.chkDistCalc.isChecked() if hasattr(self, "chkDistCalc") else False,
                "draw": self.chkDraw.isChecked() if hasattr(self, "chkDraw") else False,
                "draw_export": self.chkDrawExport.isChecked() if hasattr(self, "chkDrawExport") else False,
                "geojson_import": self.chkGeoJsonImport.isChecked() if hasattr(self, "chkGeoJsonImport") else False,
                "external_tile": self.chkExternalTile.isChecked() if hasattr(self, "chkExternalTile") else False,
                "csmap":      self.chkCsmap.isChecked()      if hasattr(self, "chkCsmap")     else False,
                "inyouzu":    self.chkInyouzu.isChecked()    if hasattr(self, "chkInyouzu")   else False,
                "mpirrim":    self.chkMpiRrim.isChecked()    if hasattr(self, "chkMpiRrim")   else False,
                "cimap":      self.chkCimap.isChecked()      if hasattr(self, "chkCimap")     else False,
                "colorrelief":self.chkColorrelief.isChecked() if hasattr(self, "chkColorrelief") else False,
                "twi":        self.chkTwi.isChecked()        if hasattr(self, "chkTwi")       else False,
                "topex":      self.chkTopex.isChecked()      if hasattr(self, "chkTopex")     else False,
                "weather":    self.chkWeather.isChecked()    if hasattr(self, "chkWeather")   else False,
                "kikikuru":     self.chkKikikuru.isChecked()    if hasattr(self, "chkKikikuru")    else False,
                "sentinel":   self.chkSentinelChange.isChecked() if hasattr(self, "chkSentinelChange")  else False,
                # 各微地形・気象レイヤの「表示」初期状態（出力ONかつ表示OFFなら初期非表示）
                "csmap_vis":       self.chkCsmapVis.isChecked()       if hasattr(self, "chkCsmapVis")       else True,
                "inyouzu_vis":     self.chkInyouzuVis.isChecked()     if hasattr(self, "chkInyouzuVis")     else True,
                "mpirrim_vis":     self.chkMpiRrimVis.isChecked()     if hasattr(self, "chkMpiRrimVis")     else True,
                "cimap_vis":       self.chkCimapVis.isChecked()       if hasattr(self, "chkCimapVis")       else True,
                "colorrelief_vis": self.chkColorreliefVis.isChecked() if hasattr(self, "chkColorreliefVis") else True,
                "twi_vis":         self.chkTwiVis.isChecked()         if hasattr(self, "chkTwiVis")         else True,
                "topex_vis":       self.chkTopexVis.isChecked()       if hasattr(self, "chkTopexVis")       else True,
                "weather_vis":     self.chkWeatherVis.isChecked()     if hasattr(self, "chkWeatherVis")     else True,
                "kikikuru_vis":    self.chkKikikuruVis.isChecked()    if hasattr(self, "chkKikikuruVis")    else True,
                # 種類2ベースマップの「表示」初期状態（チェックを外すと初期非表示で開始）
                "basemap2_vis":    self.chkBasemap2Vis.isChecked()    if hasattr(self, "chkBasemap2Vis")    else True,
                "feature_search":  self.chkFeatureSearch.isChecked()  if hasattr(self, "chkFeatureSearch")  else True,
                "terrain_3d": self.chkTerrain3D.isChecked()  if hasattr(self, "chkTerrain3D") else False,
                "split_view": self.chkSplitView.isChecked()  if hasattr(self, "chkSplitView") else False,
                "stats":      self.chkStats.isChecked()      if hasattr(self, "chkStats")     else False,
                "print":      self.chkPrint.isChecked()      if hasattr(self, "chkPrint")     else False,
                "zoning":      self.chkZoning.isChecked()      if hasattr(self, "chkZoning")     else False,
                "roadsim":    self.chkRoadSim.isChecked()   if hasattr(self, "chkRoadSim")  else False,
                "route":    self.chkRoute.isChecked()  if hasattr(self, "chkRoute")  else False,
                "cablesim":  self.chkCableSim.isChecked()  if hasattr(self, "chkCableSim")  else False,
            }

            theme_name = self.cmbTheme.currentText() if hasattr(self, "cmbTheme") else "緑系"
            theme = THEMES.get(theme_name, list(THEMES.values())[0])

            # ===== オプション ライセンス認証（永続動作モデル）=====
            # 生成時にコードを認証し、HTMLには永続トークンを埋め込む。
            code1 = self.txtLicenseOpt1.text() if hasattr(self, "txtLicenseOpt1") else ""
            code2 = self.txtLicenseOpt2.text() if hasattr(self, "txtLicenseOpt2") else ""

            # 各オプションの機能が使われているか
            opt1_used = any(opts_ui.get(k) for k in OPTION1_FEATURE_KEYS)
            opt2_treesvg_used = any(
                (l.get("kind") == "vector-tile"
                 and l.get("style", {}).get("vt-geom-type") == "Point"
                 and l.get("style", {}).get("vt-tree-svg-enabled"))
                for l in export_layers
            )
            opt2_used = bool(opts_ui.get("stats")) or opt2_treesvg_used or bool(opts_ui.get("roadsim") or bool(opts_ui.get("route")) or bool(opts_ui.get("cablesim")) ) or bool(opts_ui.get("print"))

            token1 = self._fetch_license_token(1, code1) if opt1_used else ""
            token2 = self._fetch_license_token(2, code2) if opt2_used else ""

            # 認証できなかった場合は警告（該当オプションは無効化して続行）
            warn = []
            if opt1_used and not code1.strip():
                warn.append("・オプション1の機能が選択されていますが、コードが未入力です。")
            elif opt1_used and not token1:
                warn.append("・オプション1のライセンスコードが無効、またはサーバに接続できません。")
            if opt2_used and not code2.strip():
                warn.append("・オプション2の機能が選択されていますが、コードが未入力です。")
            elif opt2_used and not token2:
                warn.append("・オプション2のライセンスコードが無効、またはサーバに接続できません。")
            if warn:
                QMessageBox.warning(
                    self, "オプション ライセンス",
                    "\n".join(warn)
                    + "\n\n認証できなかったオプションは無効化してHTMLを出力します。",
                )

            opts_ui["_server_base"] = LICENSE_SERVER_BASE
            opts_ui["_token_opt1"] = token1
            opts_ui["_token_opt2"] = token2
            # =====================================================

            html = self._build_html(title, source, basemap_name, export_layers, extent, opts_ui, theme, basemap_name2)
            with open(output_html, "w", encoding="utf-8") as f:
                f.write(html)

            QMessageBox.information(self, "完了", f"HTML を出力しました:\n{output_html}")
            self._log("DONE: " + output_html)

        except Exception:
            self._log(traceback.format_exc())
            QMessageBox.critical(self, "エラー", traceback.format_exc())

    def _build_color_expr(self, rules, field, default_color):
        """
        MapLibre GL JS の match 式または step 式を生成する。
        rules が空、またはフィールド名が空の場合は default_color（文字列）を返す。

        数値ルール（num_min / num_max キーを持つ）の場合は step 式を生成:
          ["step", ["get", field],
            fallback_color,
            lower1, color1,
            lower2, color2, ...]

        文字列ルールの場合は match 式:
          ["match", ["get", field],
            "val1", color1,
            fallback_color]
        """
        if not rules or not field:
            return default_color

        # 数値ルールかどうかを判定（いずれかの行に num_min または num_max があれば数値モード）
        is_numeric = any("num_min" in r or "num_max" in r for r in rules)

        if is_numeric:
            # step 式: ブレーク値は num_min を使用。num_min がない行はスキップ
            # step 式の構造: ["step", input, output_for_<_first_break, break1, output1, break2, output2, ...]
            # MapLibre では最初の出力が input < break1 の場合に使われる
            sorted_rules = sorted(
                [r for r in rules if "num_min" in r],
                key=lambda r: float(r["num_min"])
            )
            if not sorted_rules:
                return default_color
            expr = ["step", ["get", field], default_color]
            for r in sorted_rules:
                expr.append(float(r["num_min"]))
                expr.append(r["color"])
            return expr
        else:
            # match 式（文字列）
            # ["get", field] は属性値が null の地物に対して null を返し、match がマッチしなくなる。
            # coalesce で null を空文字列 "" に変換することで、空欄ルール（value=""）が null 地物にも適用される。
            get_expr = ["coalesce", ["get", field], ""]
            expr = ["match", get_expr]
            for rule in rules:
                expr.append(rule["value"])
                expr.append(rule["color"])
            expr.append(default_color)
            return expr

    def _build_value_expr(self, rules, field, default_value, key):
        """
        属性値ごとに数値プロパティ（不透明度・枠幅／ライン幅など）を切り替える
        MapLibre 式を生成する。各ルールが key（"opacity" / "width"）を持てばその値を、
        持たなければ default_value（上段の既定値）を使う。

        どのルールも key を上書きしていない場合は、不要な式を作らずスカラーの
        default_value をそのまま返す（＝従来どおり全地物が単一値）。

        色分け（_build_color_expr）と同じ判定で step（数値）/ match（文字列）を生成する。
        """
        if not rules or not field:
            return default_value

        def _has_override(r):
            v = r.get(key, None)
            return v is not None and v != ""

        # 1つも上書きが無ければ従来どおりスカラー
        if not any(_has_override(r) for r in rules):
            return default_value

        def _rule_val(r):
            v = r.get(key, None)
            if v is None or v == "":
                return default_value
            try:
                return float(v)
            except (ValueError, TypeError):
                return default_value

        is_numeric = any("num_min" in r or "num_max" in r for r in rules)

        if is_numeric:
            sorted_rules = sorted(
                [r for r in rules if "num_min" in r],
                key=lambda r: float(r["num_min"])
            )
            if not sorted_rules:
                return default_value
            expr = ["step", ["get", field], default_value]
            for r in sorted_rules:
                expr.append(float(r["num_min"]))
                expr.append(_rule_val(r))
            return expr
        else:
            get_expr = ["coalesce", ["get", field], ""]
            expr = ["match", get_expr]
            for rule in rules:
                expr.append(rule["value"])
                expr.append(_rule_val(rule))
            expr.append(default_value)
            return expr

    def _build_legend(self, style, geom_type=None):
        """
        レイヤのスタイル情報から凡例アイテムリストを返す。
        色分けルールがあれば各値ごとのアイテム、なければ単色アイテム1件。

        戻り値: [{"label": str, "color": str, "shape": "fill"|"line"|"circle"}, ...]
        """
        rule_enabled = style.get("vt-color-rule-enabled", False)
        rules = style.get("vt-color-rules", []) if rule_enabled else []
        field = style.get("vt-color-rule-field", "")

        # ジオメトリ種別を正規化
        gt = geom_type or style.get("vt-geom-type") or style.get("geom", "Polygon")
        if gt in ("Polygon", "polygon"):
            shape = "fill"
            default_color = style.get("fill-color", "#2d8a4e")
        elif gt in ("LineString", "linestring", "line"):
            shape = "line"
            default_color = style.get("vt-line-color") or style.get("line-color", "#1d6fa4")
        else:
            shape = "circle"
            default_color = style.get("vt-circle-color") or style.get("circle-color", "#e63946")

        if rules and field:
            items = []
            for rule in rules:
                has_num = "num_min" in rule or "num_max" in rule
                if has_num:
                    lo = rule.get("num_min", "")
                    hi = rule.get("num_max", "")
                    lo_str = str(int(lo) if isinstance(lo, float) and lo == int(lo) else lo) if lo != "" else ""
                    hi_str = str(int(hi) if isinstance(hi, float) and hi == int(hi) else hi) if hi != "" else ""
                    if lo_str and hi_str:
                        label = f"{lo_str}～{hi_str}"
                    elif lo_str:
                        label = f"{lo_str}～"
                    elif hi_str:
                        label = f"～{hi_str}"
                    else:
                        label = ""
                else:
                    label = str(rule.get("value", ""))
                items.append({"label": label, "color": rule["color"], "shape": shape})
            return items
        else:
            return [{"label": "", "color": default_color, "shape": shape}]

    def _tree_circle_radius_expr(self, center_lat):
        """
        2D黒円（フォールバック）の circle-radius 式を生成する。
        半径(m) = 樹高 * TREE_CIRCLE_RADIUS_PER_HEIGHT を実寸でとらえ、
        現在ズームでのピクセル数に換算する。

        px/m = (256 * 2^zoom) / (40075016.686 * cos(lat))
        exponential(base=2) の2点で厳密に実寸スケールになる。
        """
        import math
        lat = max(min(center_lat, 85.0), -85.0)
        cos_lat = math.cos(math.radians(lat)) or 1e-6
        circumference = 40075016.686

        def px_per_m(zoom):
            return (256.0 * (2 ** zoom)) / (circumference * cos_lat)

        h_field = TREE_SVG_FIELDS["height"]
        # 半径(m) = to-number(樹高, default) * 係数。Nodata/非数値でも default にフォールバック
        radius_m = [
            "*",
            ["to-number", ["get", h_field], TREE_SVG_DEFAULT_HEIGHT_M],
            TREE_CIRCLE_RADIUS_PER_HEIGHT,
        ]
        z0, z1 = 0, 24
        return [
            "interpolate", ["exponential", 2], ["zoom"],
            z0, ["max", 2.0, ["*", radius_m, px_per_m(z0)]],
            z1, ["max", 2.0, ["*", radius_m, px_per_m(z1)]],
        ]

    def _tree_icon_size_expr(self, center_lat):
        """
        単木symbolアイコンの icon-size 式を生成する。
        アイコンは基準高さ TREE_SVG_ICON_BASE_PX(px) で作られているので、
        画面上の樹高を 樹高(m) の実寸にするための倍率を返す（樹冠幅は使わない）。

          icon-size(z) = 樹高(m) * px/m(z) / 基準px
        px/m は中心緯度を使った定数とし、exponential(base=2) の2点で実寸スケールにする。
        """
        import math
        lat = max(min(center_lat, 85.0), -85.0)
        cos_lat = math.cos(math.radians(lat)) or 1e-6
        circumference = 40075016.686

        def factor(zoom):
            return (256.0 * (2 ** zoom)) / (circumference * cos_lat) / TREE_SVG_ICON_BASE_PX

        h_field = TREE_SVG_FIELDS["height"]
        height_m = ["to-number", ["get", h_field], TREE_SVG_DEFAULT_HEIGHT_M]
        z0, z1 = 0, 24
        m = TREE_SVG_ICON_MIN_SIZE
        return [
            "interpolate", ["exponential", 2], ["zoom"],
            z0, ["max", m, ["*", height_m, factor(z0)]],
            z1, ["max", m, ["*", height_m, factor(z1)]],
        ]

    def _tree_icon_image_expr(self):
        """
        単木symbolアイコンの icon-image 式を生成する（concat でアイコン名を組み立てる）。
          - ズーム TREE_SVG_TRUNK_MIN_ZOOM 未満: "<prefix><species>_crown"（樹冠のみ）
          - 以上: "<prefix><species>_<tier>"（樹幹+樹冠。tierは樹冠長率3段階）
        樹冠長率が Nodata/列なしのときは default 値→中位"mid"になり、分岐は実質無効になる。
        """
        species_field = TREE_SVG_FIELDS["species"]
        ratio_field = TREE_SVG_FIELDS["crown_ratio"]

        # 樹種 → スラッグ（default は末尾樹種のスラッグ）
        species_slug = ["match", ["get", species_field]]
        for jp, slug in TREE_SVG_SPECIES:
            species_slug += [jp, slug]
        species_slug += [TREE_SVG_DEFAULT_SLUG]

        # 樹冠長率 → tier スラッグ（step: <30=low, 30-50=mid, >=50=high）
        # 区間下限が None(=low) を先頭の既定出力にし、以降を stop として追加する。
        ratio_num = ["to-number", ["get", ratio_field], TREE_SVG_DEFAULT_CROWN_RATIO]
        tier_step = ["step", ratio_num]
        first = True
        for slug, _rep, lo in TREE_SVG_TIERS:
            if lo is None:
                tier_step.append(slug)        # 既定出力（下限未満）
            else:
                tier_step += [lo, slug]        # stop, 出力

        full_name = ["concat", TREE_SVG_ICON_PREFIX, species_slug, "_", tier_step]
        crown_name = ["concat", TREE_SVG_ICON_PREFIX, species_slug, "_crown"]

        return [
            "step", ["zoom"],
            crown_name,                  # 16未満: 樹冠のみ
            TREE_SVG_TRUNK_MIN_ZOOM,
            full_name,                   # 16以上: 樹幹+樹冠（tier別）
        ]

    def _build_vector_tile_layers(self, vt, center_lat=0.0):
        style = vt.get("style", {})
        source_id = vt.get("source") or vt["id"]
        source_layer = style.get("vt-source-layer", "").strip()
        geom_type = style.get("vt-geom-type", "Polygon")

        if not source_layer:
            raise ValueError(f"VectorTileのsource-layerが未指定: {vt['name']}")

        layer_defs = []
        layer_ids = []

        # 属性値色分けルール
        rule_enabled = style.get("vt-color-rule-enabled", False)
        rule_field = style.get("vt-color-rule-field", "").strip()
        color_rules = style.get("vt-color-rules", []) if rule_enabled else []

        if geom_type == "Polygon":
            fill_id = source_id + "_fill"
            fill_color = self._build_color_expr(color_rules, rule_field, style.get("fill-color", "#2d8a4e"))
            # 属性値ごとの不透明度（塗り）・枠幅（外周線幅）（空欄ルールは上段の既定値を継承）
            fill_op = self._build_value_expr(color_rules, rule_field, style.get("fill-opacity", 0.6), "opacity")
            outline_w = self._build_value_expr(color_rules, rule_field, style.get("vt-outline-width", 1.0), "width")
            layer_defs.append({
                "id": fill_id,
                "type": "fill",
                "source": source_id,
                "source-layer": source_layer,
                "paint": {
                    "fill-color": fill_color,
                    "fill-opacity": fill_op,
                },
            })
            layer_ids.append(fill_id)
            outline_id = source_id + "_outline"
            layer_defs.append({
                "id": outline_id,
                "type": "line",
                "source": source_id,
                "source-layer": source_layer,
                "paint": {
                    "line-color": style.get("vt-outline-color", "#ffffff"),
                    "line-width": outline_w,
                },
            })
            layer_ids.append(outline_id)

        elif geom_type == "LineString":
            line_id = source_id + "_line"
            line_color = self._build_color_expr(color_rules, rule_field, style.get("vt-line-color", "#1d6fa4"))
            # 属性値ごとのライン幅・不透明度（空欄ルールは上段の既定値を継承）
            line_w = self._build_value_expr(color_rules, rule_field, style.get("vt-line-width", 2.0), "width")
            line_op = self._build_value_expr(color_rules, rule_field, style.get("vt-line-opacity", 1.0), "opacity")
            layer_defs.append({
                "id": line_id,
                "type": "line",
                "source": source_id,
                "source-layer": source_layer,
                "paint": {
                    "line-color": line_color,
                    "line-width": line_w,
                    "line-opacity": line_op,
                },
            })
            layer_ids.append(line_id)

        elif geom_type == "Point":
            circle_id = source_id + "_circle"
            tree_svg_enabled = bool(style.get("vt-tree-svg-enabled", False))
            if tree_svg_enabled:
                # 単木アイコン: フォールバック(=2D常時)の円は黒・樹高由来の実寸半径。
                # 色分けルールよりも黒を優先する（アイコン/円の見え方を統一するため）。
                layer_defs.append({
                    "id": circle_id,
                    "type": "circle",
                    "source": source_id,
                    "source-layer": source_layer,
                    "paint": {
                        "circle-color": "#000000",
                        "circle-radius": self._tree_circle_radius_expr(center_lat),
                        "circle-stroke-color": "#000000",
                        "circle-stroke-width": 0,
                    },
                })
            else:
                circle_color = self._build_color_expr(color_rules, rule_field, style.get("vt-circle-color", "#e63946"))
                # 属性値ごとの枠幅（円の縁取り幅）・不透明度（空欄ルールは上段の既定値を継承）
                circle_stroke_w = self._build_value_expr(color_rules, rule_field, 1.5, "width")
                circle_opacity = self._build_value_expr(color_rules, rule_field, 1.0, "opacity")
                layer_defs.append({
                    "id": circle_id,
                    "type": "circle",
                    "source": source_id,
                    "source-layer": source_layer,
                    "paint": {
                        "circle-color": circle_color,
                        "circle-radius": style.get("vt-circle-radius", 6),
                        "circle-stroke-color": style.get("vt-circle-stroke", "#ffffff"),
                        "circle-stroke-width": circle_stroke_w,
                        "circle-opacity": circle_opacity,
                        "circle-stroke-opacity": circle_opacity,
                    },
                })
            layer_ids.append(circle_id)

        label_field = style.get("vt-label-field", "").strip()
        if style.get("vt-label-enabled") and label_field:
            label_id = source_id + "_label"
            halo_color = style.get("vt-label-halo-color", "#ffffff") if style.get("vt-label-halo", True) else "rgba(0,0,0,0)"
            halo_width = 1.5 if style.get("vt-label-halo", True) else 0
            layer_defs.append({
                "id": label_id,
                "type": "symbol",
                "source": source_id,
                "source-layer": source_layer,
                "minzoom": float(style.get("vt-label-minzoom", 0)),
                "maxzoom": float(style.get("vt-label-maxzoom", 24)),
                "layout": {
                    "text-field": ["to-string", ["get", label_field]],
                    "text-font": ["Open Sans Regular"],
                    "text-size": style.get("vt-label-size", 12),
                    "text-allow-overlap": False,
                },
                "paint": {
                    "text-color": style.get("vt-label-color", "#222222"),
                    "text-halo-color": halo_color,
                    "text-halo-width": halo_width,
                },
            })
            layer_ids.append(label_id)

        return layer_defs, layer_ids

    def _build_html(self, title, source, basemap_name, export_layers, extent, opts, theme, basemap_name2=None):
        xmin, ymin = extent.xMinimum(), extent.yMinimum()
        xmax, ymax = extent.xMaximum(), extent.yMaximum()
        cx = (xmin + xmax) / 2
        cy = (ymin + ymax) / 2

        # 出典（自動生成）: 選択ベースマップ＋データレイヤから提供元別に組み立てる。
        # 手入力の出典(source)とは独立して別欄に表示する。
        auto_source = build_data_attribution(basemap_name, basemap_name2, opts)

        # ----- オプション ライセンス（保護JSローダー用）-----
        server_base = opts.get("_server_base", LICENSE_SERVER_BASE)
        token_opt1 = opts.get("_token_opt1", "") or ""
        token_opt2 = opts.get("_token_opt2", "") or ""
        opt1_ok = bool(token_opt1)
        opt2_ok = bool(token_opt2)
        needed_opt1 = []  # サーバ option1/ から読み込む .js のファイル名
        needed_opt2 = []  # サーバ option2/ から読み込む .js のファイル名

        def _need_opt1(fn):
            if fn not in needed_opt1:
                needed_opt1.append(fn)

        def _need_opt2(fn):
            if fn not in needed_opt2:
                needed_opt2.append(fn)

        def _bm_source(bm):
            return f"""
      {{
        "type": "raster",
        "tiles": {json.dumps(bm["tiles"], ensure_ascii=False)},
        "tileSize": {bm.get("tileSize", 256)},
        "attribution": {json.dumps(bm.get("attribution", ""), ensure_ascii=False)}
      }}"""

        bm_sources = []
        bm_layers = []
        # パネル最上段＝描画最下層の慣例に合わせ、basemap(最下層)→basemap2 の順で登録
        basemap_panel_entries = []   # [(layer_id, 表示名, 初期表示), ...]
        bm1 = BASEMAPS.get(basemap_name)
        if bm1:
            bm_sources.append('"basemap":' + _bm_source(bm1))
            bm_layers.append('{"id":"basemap","type":"raster","source":"basemap"}')
            basemap_panel_entries.append(("basemap", basemap_name, True))
        bm2 = BASEMAPS.get(basemap_name2) if basemap_name2 else None
        if bm2:
            bm_sources.append('"basemap2":' + _bm_source(bm2))
            bm_layers.append('{"id":"basemap2","type":"raster","source":"basemap2"}')
            # 種類2は「表示」チェックボックスの初期状態に従う（非表示なら初期OFFで開始）
            basemap_panel_entries.append(("basemap2", basemap_name2, opts.get("basemap2_vis", True)))
        bm_source_js = ",".join(bm_sources)
        bm_layer_js = ",".join(bm_layers)

        load_js = ""
        panel_js = ""
        popup_layer_ids = []

        # ---- レイヤ重なり順／パネル表示順の制御用アキュムレータ ----
        # 描画(重なり)は「下から: ベースマップ → 微地形 → QGIS → 外部 → 気象」、
        # パネル表示順は QGIS の既存慣例（パネル上段＝描画下層）に統一する。
        #   load_js  : map.on("load") 内で実行。先に実行＝先に addLayer＝描画下層。
        #   panel_js : addToggle 呼び出し列。先に呼ぶ＝パネル上段。
        panel_qgis_js = ""        # QGISレイヤのパネルトグル
        panel_basemap_js = ""     # ベースマップのパネルトグル（パネル最上段＝描画最下層）
        micro_init = {}           # 微地形系 init JS（キー: 機能名）
        micro_panel = {}          # 微地形系 パネルトグル JS（キー: 機能名）
        sentinel_init_js = ""     # Sentinel-2 変化解析 init JS（オプション1）
        sentinel_panel_js = ""    # 未使用（Sentinelは左ツールボタンで操作）       
        kikikuru_init_js = ""     # キキクル init JS
        kikikuru_panel_js = ""    # キキクル パネルトグル
        weather_init_js = ""      # 気象 init JS（描画最上層＝最後に addLayer）
        weather_panel_js = ""     # 気象 パネルトグル（パネル最下段）
        terrain_init_js = ""      # 3D地形 sky レイヤ init

        # QGIS legend order is top-to-bottom. MapLibre draws later layers on top,
        # so add map layers bottom-to-top.
        all_layers = []
        fgb_calls = []  # (layerId, url, styleLayers) を後でまとめてemit
        layer_id_groups = []
        treesvg_layers = []  # 単木SVGアイコン有効な Point VectorTile レイヤ情報

        for layer in export_layers:

            if layer["kind"] == "raster":
                load_js += f"""
          map.addSource({json.dumps(layer["id"])}, {{
            type: "raster",
            tiles: [{json.dumps(layer["url"])}],
            tileSize: 256
          }});
        """
                r_style = layer.get("style", {})
                raster_layer_def = {
                    "id": layer["id"],
                    "type": "raster",
                    "source": layer["id"],
                    "minzoom": float(r_style.get("minzoom", 0)),
                    "maxzoom": float(r_style.get("maxzoom", 24)),
                    "paint": {
                        "raster-opacity": float(r_style.get("raster-opacity", 1.0)),
                    },
                }
                all_layers.append(raster_layer_def)
                layer_id_groups.append(([layer["id"]], layer["name"], "raster", [], layer.get("initial_visible", True)))

            elif layer["kind"] == "vector-tile":
                load_js += f"""
          map.addSource({json.dumps(layer["source"])}, {{
            type: "vector",
            tiles: [{json.dumps(layer["url"])}]
          }});
        """
                vt_defs, vt_ids = self._build_vector_tile_layers(layer, center_lat=cy)

                for ld in vt_defs:
                    all_layers.append(ld)

                vt_legend = self._build_legend(layer.get("style", {}))
                layer_id_groups.append((vt_ids, layer["name"], "vector-tile", vt_legend, layer.get("initial_visible", True)))

                # 単木SVGアイコンが有効な Point レイヤを収集（後で treesvg 連携JSをemit）
                vt_style = layer.get("style", {})
                if vt_style.get("vt-geom-type") == "Point" and vt_style.get("vt-tree-svg-enabled"):
                    _src = layer.get("source") or layer["id"]
                    treesvg_layers.append({
                        "circle_id": _src + "_circle",
                        "symbol_id": _src + "_tree",
                        "source": _src,
                        "source_layer": vt_style.get("vt-source-layer", "").strip(),
                    })

                # ベクトルタイルのポップアップ対象レイヤIDを追加
                # symbolレイヤ(ラベル)以外をポップアップ対象に
                for ld in vt_defs:
                    if ld.get("type") != "symbol":
                        popup_layer_ids.append((ld["id"], "vector-tile", layer.get("style", {}).get("vt-source-layer", "")))

            elif layer["kind"] == "fgb":
                style = layer["style"]
                geom = layer["geom"]
                lid = layer["id"]
                url = layer["url"]

                # スタイルレイヤ定義を構築（addLayerはloadFgbLayerに委譲）
                fgb_style_layers = []
                ids = []

                # 属性値色分けルール（fgb は vt- プレフィックスなしのキー共用）
                rule_enabled = style.get("vt-color-rule-enabled", False)
                rule_field = style.get("vt-color-rule-field", "").strip()
                color_rules = style.get("vt-color-rules", []) if rule_enabled else []

                if geom == "Point":
                    circle_color = self._build_color_expr(color_rules, rule_field, style.get("circle-color", "#e63946"))
                    # 属性値ごとの枠幅（円の縁取り幅）・不透明度（空欄ルールは上段の既定値を継承）
                    circle_stroke_w = self._build_value_expr(color_rules, rule_field, style.get("circle-stroke-width", 1.5), "width")
                    circle_opacity = self._build_value_expr(color_rules, rule_field, 1.0, "opacity")
                    fgb_style_layers.append({
                        "id": lid,
                        "type": "circle",
                        "source": lid,
                        "minzoom": float(style.get("minzoom", 0)),
                        "maxzoom": float(style.get("maxzoom", 24)),
                        "paint": {
                            "circle-color": circle_color,
                            "circle-radius": style.get("circle-radius", 8),
                            "circle-stroke-color": style.get("circle-stroke-color", "#ffffff"),
                            "circle-stroke-width": circle_stroke_w,
                            "circle-opacity": circle_opacity,
                            "circle-stroke-opacity": circle_opacity,
                        }
                    })
                    ids.append(lid)
                    popup_layer_ids.append(lid)

                elif geom == "LineString":
                    line_color = self._build_color_expr(color_rules, rule_field, style.get("line-color", "#1d6fa4"))
                    # 属性値ごとのライン幅・不透明度（空欄ルールは上段の既定値を継承）
                    line_w = self._build_value_expr(color_rules, rule_field, style.get("line-width", 2.0), "width")
                    line_op = self._build_value_expr(color_rules, rule_field, style.get("line-opacity", 1.0), "opacity")
                    fgb_style_layers.append({
                        "id": lid,
                        "type": "line",
                        "source": lid,
                        "minzoom": float(style.get("minzoom", 0)),
                        "maxzoom": float(style.get("maxzoom", 24)),
                        "paint": {
                            "line-color": line_color,
                            "line-width": line_w,
                            "line-opacity": line_op,
                        }
                    })
                    ids.append(lid)
                    popup_layer_ids.append(lid)

                else:
                    fill_id = lid
                    outline_id = lid + "_outline"
                    fill_color = self._build_color_expr(color_rules, rule_field, style.get("fill-color", "#2d8a4e"))
                    # 属性値ごとの不透明度（塗り）・枠幅（外周線幅）（空欄ルールは上段の既定値を継承）
                    fill_op = self._build_value_expr(color_rules, rule_field, style.get("fill-opacity", 0.5), "opacity")
                    outline_w = self._build_value_expr(color_rules, rule_field, style.get("line-width", 1.0), "width")
                    fgb_style_layers.append({
                        "id": fill_id,
                        "type": "fill",
                        "source": lid,
                        "minzoom": float(style.get("minzoom", 0)),
                        "maxzoom": float(style.get("maxzoom", 24)),
                        "paint": {
                            "fill-color": fill_color,
                            "fill-opacity": fill_op,
                        }
                    })
                    fgb_style_layers.append({
                        "id": outline_id,
                        "type": "line",
                        "source": lid,
                        "minzoom": float(style.get("minzoom", 0)),
                        "maxzoom": float(style.get("maxzoom", 24)),
                        "paint": {
                            "line-color": style.get("fill-outline-color", "#ffffff"),
                            "line-width": outline_w,
                            "line-opacity": style.get("line-opacity", 1.0),
                        }
                    })
                    ids.extend([fill_id, outline_id])
                    popup_layer_ids.append(fill_id)

                label_field = style.get("label-field", "")
                if style.get("label-enabled") and label_field:
                    label_id = lid + "_label"
                    fgb_style_layers.append({
                        "id": label_id,
                        "type": "symbol",
                        "source": lid,
                        "minzoom": float(style.get("text-minzoom", 0)),
                        "maxzoom": float(style.get("text-maxzoom", 24)),
                        "layout": {
                            "text-field": ["to-string", ["get", label_field]],
                            "text-font": ["Open Sans Regular"],
                            "text-size": style.get("text-size", 12),
                            "text-allow-overlap": False,
                        },
                        "paint": {
                            "text-color": style.get("text-color", "#222222"),
                            "text-halo-color": style.get("text-halo-color", "#ffffff"),
                            "text-halo-width": style.get("text-halo-width", 1.5),
                        }
                    })
                    ids.append(label_id)

                # ループ後にまとめてemitするためfgb_callsに積む（順序制御のため）
                fgb_calls.append((lid, url, fgb_style_layers, layer.get("initial_visible", True)))

                fgb_legend = self._build_legend(style, geom)
                layer_id_groups.append((ids, layer["name"], geom.lower(), fgb_legend, layer.get("initial_visible", True)))

        # raster / vector-tile を先に addLayer（描画順の下層）
        for ld in all_layers:
            load_js += f"  map.addLayer({json.dumps(ld, ensure_ascii=False)});\n"

        # fgb レイヤは raster/VT の addLayer が完了した後に loadFgbLayer を呼ぶ
        # → MapLibre は後から addLayer したものが上に描画されるため、
        #   fgb（ベクタ）が必ずラスタより上になる
        for (fgb_lid, fgb_url, fgb_style, fgb_vis) in fgb_calls:
            load_js += f"  await loadFgbLayer(map, {json.dumps(fgb_lid)}, {json.dumps(fgb_url, ensure_ascii=False)}, {json.dumps(fgb_style, ensure_ascii=False)}, {'true' if fgb_vis else 'false'});\n"

        # Keep the layer panel in the same top-to-bottom order as QGIS.
        # (vector-tile entries are already added inline above; skip them here)
        for ids, name, kind, legend, init_vis in layer_id_groups:
            panel_qgis_js += f"  addToggle({json.dumps(ids)}, {json.dumps(name, ensure_ascii=False)}, {json.dumps(kind)}, {json.dumps(legend, ensure_ascii=False)}, {'true' if init_vis else 'false'});\n"

        popup_js = ""
        if opts["popup"] and popup_layer_ids:
            # GeoJSONレイヤIDリスト（文字列）とVTレイヤIDリスト（タプル）を分離
            geojson_ids = [item for item in popup_layer_ids if isinstance(item, str)]
            vt_entries  = [item for item in popup_layer_ids if isinstance(item, tuple)]
            vt_ids_only = [item[0] for item in vt_entries]
            all_popup_ids = geojson_ids + vt_ids_only

            # ポイントレイヤIDを抽出（bboxクエリ優先処理のため）
            point_ids = []
            for layer in export_layers:
                if layer["kind"] == "fgb" and layer.get("geom") == "Point":
                    point_ids.append(layer["id"])

            popup_js = """
const POPUP_LAYERS = %s;
const VT_POPUP_LAYERS = %s;
const POINT_LAYERS = %s;

const popup = new maplibregl.Popup({
  closeButton: true,
  maxWidth: "320px"
});

function buildPopupHTML(props) {
  const rows = Object.entries(props)
    .filter(([k]) => !k.startsWith("_"))
    .map(([key, value]) => `<tr><td class="pk">${key}</td><td>${value ?? ""}</td></tr>`)
    .join("");
  return rows ? `<table class="ptbl">${rows}</table>` : "<em>属性なし</em>";
}

// ポイントレイヤ: 広めのbbox（±10px）で感度を高め、ポリゴンより優先処理
POINT_LAYERS.forEach(layerId => {
  map.on("click", e => {
    if (window.__statsActive) return;  // 属性集計モード中はポップアップ抑止
    const bbox = [
      [e.point.x - 10, e.point.y - 10],
      [e.point.x + 10, e.point.y + 10]
    ];
    const features = map.queryRenderedFeatures(bbox, { layers: [layerId] });
    if (!features || !features.length) return;
    const props = features[0].properties || {};
    popup.setLngLat(e.lngLat)
      .setHTML(buildPopupHTML(props))
      .addTo(map);
    e.originalEvent._pointPopupShown = true;  // ポリゴン側でスキップするフラグ
  });
  map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
});

// ポイント以外のレイヤ: ポイントポップアップが既に表示されていたらスキップ
const NON_POINT_LAYERS = POPUP_LAYERS.filter(id => !POINT_LAYERS.includes(id));
NON_POINT_LAYERS.forEach(layerId => {
  map.on("click", layerId, e => {
    if (window.__statsActive) return;  // 属性集計モード中はポップアップ抑止
    if (e.originalEvent._pointPopupShown) return;  // ポイント優先
    if (!e.features || !e.features.length) return;
    const props = e.features[0].properties || {};
    popup.setLngLat(e.lngLat)
      .setHTML(buildPopupHTML(props))
      .addTo(map);
  });
  map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
});

VT_POPUP_LAYERS.forEach(layerId => {
  map.on("click", layerId, e => {
    if (window.__statsActive) return;  // 属性集計モード中はポップアップ抑止
    if (e.originalEvent._pointPopupShown) return;
    if (!e.features || !e.features.length) return;
    const props = e.features[0].properties || {};
    popup.setLngLat(e.lngLat)
      .setHTML(buildPopupHTML(props))
      .addTo(map);
  });
  map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
});
""" % (json.dumps(geojson_ids), json.dumps(vt_ids_only), json.dumps(point_ids))

        zoom_ctrl_js = 'map.addControl(new maplibregl.NavigationControl(), "top-right");' if opts["zoom_ctrl"] else ""
        scale_js = 'map.addControl(new maplibregl.ScaleControl({maxWidth:120,unit:"metric"}), "bottom-left");' if opts["scale"] else ""

        coord_js = """
map.on("mousemove", e => {
  document.getElementById("coord-cell").textContent =
    "経度: " + e.lngLat.lng.toFixed(5) + "  緯度: " + e.lngLat.lat.toFixed(5);
});""" if opts["coord"] else ""

        # 現在ズームレベルの表示（緯度経度の上・地図との間の欄）。常時表示。
        zoom_js = """
function _updateZoomCell(){
  var el = document.getElementById("zoom-cell");
  if(!el) return;
  el.textContent = "ズームレベル：" + (Math.round(map.getZoom() * 10) / 10).toFixed(1);
}
map.on("zoom", _updateZoomCell);
map.on("load", _updateZoomCell);
_updateZoomCell();"""

        addr_html = """
<button class="ctrl-btn" id="btn-addr" onclick="toggleAddr()">📒 住所検索</button>
<div id="addr-panel" class="addr-panel">
  <div class="addr-row">
    <input id="addr-input" type="text" placeholder="住所・地名を入力" />
    <button onclick="searchAddress()">検索</button>
  </div>
  <ul id="addr-results" class="addr-results"></ul>
</div>""" if opts["addr_search"] else ""

        gps_html = '<button class="ctrl-btn" onclick="getLocation()">📌 現在地</button>' if opts["gps"] else ""
        share_html = '<button class="ctrl-btn" onclick="copyShareLink()">🔗 地図共有リンク</button>' if opts.get("share_link", True) else ""

        addr_js = """
function toggleAddr(){
  document.getElementById("addr-panel").classList.toggle("open");
}
async function searchAddress(){
  const q = document.getElementById("addr-input").value.trim();
  const list = document.getElementById("addr-results");
  if(!q) return;
  list.innerHTML = '<li class="addr-msg">検索中…</li>';
  let data;
  try{
    const res = await fetch("https://msearch.gsi.go.jp/address-search/AddressSearch?q=" + encodeURIComponent(q));
    data = await res.json();
  }catch(e){
    list.innerHTML = '<li class="addr-msg">検索に失敗しました</li>';
    return;
  }
  if(!Array.isArray(data) || !data.length){
    list.innerHTML = '<li class="addr-msg">住所が見つかりませんでした</li>';
    return;
  }
  list.innerHTML = "";
  data.forEach(function(f){
    const c = f.geometry && f.geometry.coordinates;
    if(!c) return;
    const title = (f.properties && f.properties.title) || (c[1].toFixed(5) + ", " + c[0].toFixed(5));
    const li = document.createElement("li");
    li.textContent = title;
    li.onclick = function(){ goToAddr(c[0], c[1], title); };
    list.appendChild(li);
  });
  // 候補が1件だけなら従来どおり自動でジャンプ
  if(data.length === 1){
    const c = data[0].geometry.coordinates;
    goToAddr(c[0], c[1], (data[0].properties && data[0].properties.title) || "");
  }
}
function goToAddr(lng, lat, title){
  map.flyTo({ center: [lng, lat], zoom: 14 });
  if(window._addrMk) window._addrMk.remove();
  window._addrMk = new maplibregl.Marker({ color: "#c84b31" })
    .setLngLat([lng, lat])
    .setPopup(new maplibregl.Popup().setText(title || ""))
    .addTo(map)
    .togglePopup();
}
document.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("addr-input");
  if(input) input.addEventListener("keydown", e => { if(e.key === "Enter") searchAddress(); });
});""" if opts["addr_search"] else ""

        gps_js = """
function getLocation(){
  if(!navigator.geolocation){ alert("現在地を取得できません"); return; }
  navigator.geolocation.getCurrentPosition(pos => {
    const { longitude: lng, latitude: lat } = pos.coords;
    map.flyTo({ center: [lng, lat], zoom: 14 });
    if(window._gpsMk) window._gpsMk.remove();
    window._gpsMk = new maplibregl.Marker({ color: "#2d8a4e" })
      .setLngLat([lng, lat])
      .setPopup(new maplibregl.Popup().setText("現在地"))
      .addTo(map)
      .togglePopup();
  }, err => alert("現在地取得失敗: " + err.message));
}""" if opts["gps"] else ""

        share_js = """
// 地図共有リンク: 表示位置に加え、レイヤパネルの表示設定（ON/OFF・不透明度・
// ベースマップ選択）も記録して復元する。
// ハッシュ形式: #zoom/lng/lat[/v<可視ビット列>][/o<idx.値-idx.値...>]
function parseSharedView(){
  const hash = window.location.hash.replace(/^#/, "");
  let parts = hash.split("/");
  let zoom, lng, lat, rest = [];
  if(parts.length >= 3 && parts[0] !== "" && !isNaN(Number(parts[0]))){
    zoom = Number(parts[0]); lng = Number(parts[1]); lat = Number(parts[2]);
    rest = parts.slice(3);
  } else {
    // 旧式（パス末尾 .html/zoom/lng/lat）との互換
    const m = window.location.pathname.match(/\\.html\\/(\\d+(?:\\.\\d+)?)\\/(-?\\d+(?:\\.\\d+)?)\\/(-?\\d+(?:\\.\\d+)?)\\/?$/);
    if(!m) return null;
    zoom = Number(m[1]); lng = Number(m[2]); lat = Number(m[3]);
  }
  if(!Number.isFinite(zoom) || !Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  if(lng < -180 || lng > 180 || lat < -90 || lat > 90) return null;

  // レイヤ表示設定セグメントを抽出
  let st = null;
  rest.forEach(p => {
    if(p.charAt(0) === "v"){ st = st || {}; st.vis = p.slice(1); }
    else if(p.charAt(0) === "o"){ st = st || {}; st.opac = p.slice(1); }
  });
  if(st) window.__sharedLayerState = st;

  return { center: [lng, lat], zoom };
}

function _encodeLayerState(){
  const tg = window.__toggles || [];
  if(!tg.length) return "";
  let vis = "", opac = [];
  tg.forEach((t, i) => {
    vis += (t.checkbox && t.checkbox.checked) ? "1" : "0";
    if(t.slider){
      const ov = Math.round(Number(t.slider.value));
      if(ov !== 100) opac.push(i + "." + ov);
    }
  });
  let seg = "";
  if(/0/.test(vis)) seg += "/v" + vis;          // すべて表示中なら省略
  if(opac.length)   seg += "/o" + opac.join("-");
  return seg;
}

function applySharedLayerState(){
  const st = window.__sharedLayerState;
  if(!st) return;
  const tg = window.__toggles || [];
  if(st.vis){
    for(let i = 0; i < tg.length && i < st.vis.length; i++){
      const want = st.vis.charAt(i) === "1";
      const cb = tg[i].checkbox;
      if(cb && cb.checked !== want){
        cb.checked = want;
        cb.dispatchEvent(new Event("change"));
      }
    }
  }
  if(st.opac){
    st.opac.split("-").forEach(pair => {
      const dot = pair.indexOf(".");
      if(dot < 0) return;
      const i = Number(pair.slice(0, dot)), v = Number(pair.slice(dot + 1));
      const t = tg[i];
      if(t && t.slider && Number.isFinite(v)){
        t.slider.value = String(v);
        t.slider.dispatchEvent(new Event("input"));
      }
    });
  }
}
window.applySharedLayerState = applySharedLayerState;

function currentShareLink(){
  const c = map.getCenter();
  const z = Math.round(map.getZoom() * 100) / 100;
  const lng = Math.round(c.lng * 100000) / 100000;
  const lat = Math.round(c.lat * 100000) / 100000;
  const base = window.location.href.split("#")[0].replace(/\\.html\\/.*$/, ".html");
  return `${base}#${z}/${lng}/${lat}${_encodeLayerState()}`;
}

async function copyShareLink(){
  const url = currentShareLink();
  try {
    await navigator.clipboard.writeText(url);
    alert("地図共有リンクをコピーしました\\n" + url);
  } catch(e) {
    window.prompt("地図共有リンクをコピーしてください", url);
  }
}
""" if opts.get("share_link", True) else ""
        shared_view_expr = "parseSharedView()" if opts.get("share_link", True) else "null"

        # --- 面積計算 ---
        area_calc_html = '<button class="ctrl-btn" id="btn-area" onclick="toggleAreaMode()">📐 面積計算</button>' if opts["area_calc"] else ""
        area_calc_js = """
// ========== 面積計算 ==========
let _areaMode = false;
let _areaPoints = [];
let _areaMarkers = [];

function toggleAreaMode() {
  _areaMode = !_areaMode;
  document.getElementById("btn-area").style.background = _areaMode ? "var(--green-dark)" : "var(--green)";
  map.getCanvas().style.cursor = _areaMode ? "crosshair" : "";
  if (!_areaMode) { _clearArea(); }
}

function _clearArea() {
  _areaPoints = [];
  _areaMarkers.forEach(m => m.remove());
  _areaMarkers = [];
  if (map.getSource("_area-src")) {
    map.getSource("_area-src").setData({ type: "FeatureCollection", features: [] });
  }
  const el = document.getElementById("_area-result");
  if (el) el.remove();
}

map.on("load", () => {
  map.addSource("_area-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "_area-fill", type: "fill", source: "_area-src",
    paint: { "fill-color": "#ff6600", "fill-opacity": 0.25 } });
  map.addLayer({ id: "_area-line", type: "line", source: "_area-src",
    paint: { "line-color": "#ff6600", "line-width": 2 } });
});

map.on("click", e => {
  if (!_areaMode) return;
  _areaPoints.push([e.lngLat.lng, e.lngLat.lat]);
  const mk = new maplibregl.Marker({ color: "#ff6600", scale: 0.6 }).setLngLat(e.lngLat).addTo(map);
  _areaMarkers.push(mk);
  _updateAreaDraw();
});

map.on("dblclick", e => {
  if (!_areaMode) return;
  e.preventDefault();
  if (_areaPoints.length >= 3) { _showAreaResult(); }
});

function _updateAreaDraw() {
  if (_areaPoints.length < 2) return;
  const coords = [..._areaPoints, _areaPoints[0]];
  map.getSource("_area-src").setData({ type: "FeatureCollection", features: [
    { type: "Feature", geometry: { type: "Polygon", coordinates: [coords] }, properties: {} }
  ]});
}

function _showAreaResult() {
  const pts = _areaPoints;
  // Shoelace formula (spherical approximation in degrees → m²)
  function sphericalArea(ring) {
    const R = 6371000;
    let area = 0;
    const n = ring.length;
    for (let i = 0; i < n; i++) {
      const [x1, y1] = ring[i].map(v => v * Math.PI / 180);
      const [x2, y2] = ring[(i+1) % n].map(v => v * Math.PI / 180);
      area += (x2 - x1) * (2 + Math.sin(y1) + Math.sin(y2));
    }
    return Math.abs(area * R * R / 2);
  }
  const m2 = sphericalArea(pts);
  const label = m2 >= 10000 ? (m2 / 10000).toFixed(2) + " ha" : m2.toFixed(1) + " m²";

  let el = document.getElementById("_area-result");
  if (!el) {
    el = document.createElement("div");
    el.id = "_area-result";
    el.style.cssText = "position:absolute;bottom:60px;left:10px;z-index:30;background:rgba(255,255,255,0.95);padding:6px 12px;border-radius:4px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,0.3)";
    document.getElementById("map").appendChild(el);
  }
  el.innerHTML = "📐 面積: <b>" + label + "</b> &nbsp;<button onclick='_clearArea()' style='font-size:11px;cursor:pointer'>×クリア</button>";
}
""" if opts["area_calc"] else ""

        # --- 距離計算 ---
        dist_calc_html = '<button class="ctrl-btn" id="btn-dist" onclick="toggleDistMode()">📏 距離計算</button>' if opts["dist_calc"] else ""
        dist_calc_js = """
// ========== 距離計算 ==========
let _distMode = false;
let _distPoints = [];
let _distMarkers = [];

function toggleDistMode() {
  _distMode = !_distMode;
  document.getElementById("btn-dist").style.background = _distMode ? "var(--green-dark)" : "var(--green)";
  map.getCanvas().style.cursor = _distMode ? "crosshair" : "";
  if (!_distMode) { _clearDist(); }
}

function _clearDist() {
  _distPoints = [];
  _distMarkers.forEach(m => m.remove());
  _distMarkers = [];
  if (map.getSource("_dist-src")) {
    map.getSource("_dist-src").setData({ type: "FeatureCollection", features: [] });
  }
  const el = document.getElementById("_dist-result");
  if (el) el.remove();
}

map.on("load", () => {
  map.addSource("_dist-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "_dist-line", type: "line", source: "_dist-src",
    paint: { "line-color": "#0066ff", "line-width": 2, "line-dasharray": [2, 2] } });
});

map.on("click", e => {
  if (!_distMode) return;
  _distPoints.push([e.lngLat.lng, e.lngLat.lat]);
  const mk = new maplibregl.Marker({ color: "#0066ff", scale: 0.6 }).setLngLat(e.lngLat).addTo(map);
  _distMarkers.push(mk);
  _updateDistDraw();
});

map.on("dblclick", e => {
  if (!_distMode) return;
  e.preventDefault();
  if (_distPoints.length >= 2) { _showDistResult(); }
});

function _haversine(a, b) {
  const R = 6371000;
  const dLat = (b[1] - a[1]) * Math.PI / 180;
  const dLng = (b[0] - a[0]) * Math.PI / 180;
  const s = Math.sin(dLat/2)**2 + Math.cos(a[1]*Math.PI/180)*Math.cos(b[1]*Math.PI/180)*Math.sin(dLng/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1-s));
}

function _updateDistDraw() {
  if (_distPoints.length < 2) return;
  map.getSource("_dist-src").setData({ type: "FeatureCollection", features: [
    { type: "Feature", geometry: { type: "LineString", coordinates: _distPoints }, properties: {} }
  ]});
}

function _showDistResult() {
  let total = 0;
  for (let i = 0; i < _distPoints.length - 1; i++) {
    total += _haversine(_distPoints[i], _distPoints[i+1]);
  }
  const label = total >= 1000 ? (total / 1000).toFixed(3) + " km" : total.toFixed(1) + " m";

  let el = document.getElementById("_dist-result");
  if (!el) {
    el = document.createElement("div");
    el.id = "_dist-result";
    el.style.cssText = "position:absolute;bottom:60px;left:10px;z-index:30;background:rgba(255,255,255,0.95);padding:6px 12px;border-radius:4px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,0.3)";
    document.getElementById("map").appendChild(el);
  }
  el.innerHTML = "📏 距離: <b>" + label + "</b> &nbsp;<button onclick='_clearDist()' style='font-size:11px;cursor:pointer'>×クリア</button>";
}
""" if opts["dist_calc"] else ""

        # --- 作図機能 ---
        draw_buttons = []
        if opts["draw"]:
            draw_buttons.append('<button class="ctrl-btn" id="btn-draw-point" onclick="setDrawMode(\'point\')">✏️ ポイントを描く</button>')
            draw_buttons.append('<button class="ctrl-btn" id="btn-draw-line" onclick="setDrawMode(\'line\')">✏️ ラインを描く</button>')
            draw_buttons.append('<button class="ctrl-btn" id="btn-draw-poly" onclick="setDrawMode(\'polygon\')">✏️ ポリゴンを描く</button>')
            draw_buttons.append('<button class="ctrl-btn" onclick="clearDraw()">🗑️ 作図クリア</button>')
        draw_html = "\n".join(draw_buttons)

        if opts["draw_export"] and not opts["draw"]:
            draw_export_html = ""  # 作図機能なしでは出力ボタンも不要
        elif opts["draw_export"]:
            draw_export_html = '<button class="ctrl-btn" onclick="exportDrawData()">💾 作図を保存</button>'
        else:
            draw_export_html = ""

        draw_js = """
// ========== 作図機能 ==========
let _drawMode = null;
let _drawFeatures = [];
let _drawTempPoints = [];
let _drawTempMarkers = [];
const _DRAW_BTNS = { point: "btn-draw-point", line: "btn-draw-line", polygon: "btn-draw-poly" };

map.on("load", () => {
  map.addSource("_draw-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "_draw-fill", type: "fill", source: "_draw-src",
    filter: ["==", "$type", "Polygon"],
    paint: { "fill-color": "#9933ff", "fill-opacity": 0.3 } });
  map.addLayer({ id: "_draw-line", type: "line", source: "_draw-src",
    filter: ["in", "$type", "LineString", "Polygon"],
    paint: { "line-color": "#9933ff", "line-width": 2 } });
  map.addLayer({ id: "_draw-circle", type: "circle", source: "_draw-src",
    filter: ["==", "$type", "Point"],
    paint: { "circle-color": "#9933ff", "circle-radius": 6, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } });
});

function setDrawMode(mode) {
  // 未完了の一時描画をキャンセル
  _cancelDrawTemp();
  if (_drawMode === mode) {
    _drawMode = null;
    map.getCanvas().style.cursor = "";
    Object.values(_DRAW_BTNS).forEach(id => {
      const el = document.getElementById(id); if(el) el.style.background = "var(--green)";
    });
    return;
  }
  _drawMode = mode;
  map.getCanvas().style.cursor = "crosshair";
  Object.entries(_DRAW_BTNS).forEach(([m, id]) => {
    const el = document.getElementById(id);
    if (el) el.style.background = m === mode ? "var(--green-dark)" : "var(--green)";
  });
}

function _cancelDrawTemp() {
  _drawTempPoints = [];
  _drawTempMarkers.forEach(m => m.remove());
  _drawTempMarkers = [];
}

function _refreshDraw() {
  map.getSource("_draw-src").setData({ type: "FeatureCollection", features: _drawFeatures });
}

map.on("click", e => {
  if (!_drawMode) return;
  if (_drawMode === "point") {
    _drawFeatures.push({ type: "Feature", geometry: { type: "Point", coordinates: [e.lngLat.lng, e.lngLat.lat] }, properties: {} });
    _refreshDraw();
    return;
  }
  _drawTempPoints.push([e.lngLat.lng, e.lngLat.lat]);
  const mk = new maplibregl.Marker({ color: "#9933ff", scale: 0.6 }).setLngLat(e.lngLat).addTo(map);
  _drawTempMarkers.push(mk);
});

map.on("dblclick", e => {
  if (!_drawMode || _drawMode === "point") return;
  e.preventDefault();
  const pts = _drawTempPoints;
  if (_drawMode === "line" && pts.length >= 2) {
    _drawFeatures.push({ type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} });
  } else if (_drawMode === "polygon" && pts.length >= 3) {
    _drawFeatures.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [[...pts, pts[0]]] }, properties: {} });
  }
  _cancelDrawTemp();
  _refreshDraw();
});

function clearDraw() {
  _cancelDrawTemp();
  _drawFeatures = [];
  _refreshDraw();
  setDrawMode(null);
}
""" if opts["draw"] else ""

        # ===== GeoJSON <-> KML/GPX 変換ユーティリティ（作図保存・外部読込で共有） =====
        geoio_js = """
// ========== GeoJSON <-> KML / GPX 変換 ==========
function _xmlEsc(s){ return String(s==null?"":s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&apos;"}[c]; }); }
function _n6(n){ return Math.round(n*1e6)/1e6; }

// --- GeoJSON features -> KML ---
function featuresToKML(features){
  function cs(c){ return _n6(c[0])+","+_n6(c[1])+","+(c.length>2&&isFinite(c[2])?_n6(c[2]):0); }
  function ring(r){ return r.map(cs).join(" "); }
  function polyKML(rings){
    var o="<outerBoundaryIs><LinearRing><coordinates>"+ring(rings[0])+"</coordinates></LinearRing></outerBoundaryIs>";
    var inr=rings.slice(1).map(function(r){ return "<innerBoundaryIs><LinearRing><coordinates>"+ring(r)+"</coordinates></LinearRing></innerBoundaryIs>"; }).join("");
    return "<Polygon>"+o+inr+"</Polygon>";
  }
  function geomKML(g){
    if(!g) return "";
    if(g.type==="Point") return "<Point><coordinates>"+cs(g.coordinates)+"</coordinates></Point>";
    if(g.type==="MultiPoint") return "<MultiGeometry>"+g.coordinates.map(function(c){return "<Point><coordinates>"+cs(c)+"</coordinates></Point>";}).join("")+"</MultiGeometry>";
    if(g.type==="LineString") return "<LineString><tessellate>1</tessellate><coordinates>"+ring(g.coordinates)+"</coordinates></LineString>";
    if(g.type==="MultiLineString") return "<MultiGeometry>"+g.coordinates.map(function(l){return "<LineString><tessellate>1</tessellate><coordinates>"+ring(l)+"</coordinates></LineString>";}).join("")+"</MultiGeometry>";
    if(g.type==="Polygon") return polyKML(g.coordinates);
    if(g.type==="MultiPolygon") return "<MultiGeometry>"+g.coordinates.map(polyKML).join("")+"</MultiGeometry>";
    if(g.type==="GeometryCollection") return "<MultiGeometry>"+g.geometries.map(geomKML).join("")+"</MultiGeometry>";
    return "";
  }
  var pms=features.map(function(f,i){
    var nm=(f.properties&&(f.properties.name||f.properties.title))||("feature"+(i+1));
    return "<Placemark><name>"+_xmlEsc(nm)+"</name>"+geomKML(f.geometry)+"</Placemark>";
  }).join("");
  return '<?xml version="1.0" encoding="UTF-8"?>\\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'+pms+'</Document></kml>';
}

// --- GeoJSON features -> GPX（点=wpt, 線=trk, ポリゴン=各リングを閉trk） ---
function featuresToGPX(features){
  function ele(c){ return (c.length>2&&isFinite(c[2]))?"<ele>"+_n6(c[2])+"</ele>":""; }
  function wpt(c){ return '<wpt lat="'+_n6(c[1])+'" lon="'+_n6(c[0])+'">'+ele(c)+'</wpt>'; }
  function trk(coords){ return "<trk><trkseg>"+coords.map(function(c){ return '<trkpt lat="'+_n6(c[1])+'" lon="'+_n6(c[0])+'">'+ele(c)+'</trkpt>'; }).join("")+"</trkseg></trk>"; }
  var body="";
  features.forEach(function(f){
    var g=f.geometry; if(!g) return;
    if(g.type==="Point") body+=wpt(g.coordinates);
    else if(g.type==="MultiPoint") g.coordinates.forEach(function(c){body+=wpt(c);});
    else if(g.type==="LineString") body+=trk(g.coordinates);
    else if(g.type==="MultiLineString") g.coordinates.forEach(function(l){body+=trk(l);});
    else if(g.type==="Polygon") g.coordinates.forEach(function(r){body+=trk(r);});
    else if(g.type==="MultiPolygon") g.coordinates.forEach(function(p){p.forEach(function(r){body+=trk(r);});});
  });
  return '<?xml version="1.0" encoding="UTF-8"?>\\n<gpx version="1.1" creator="ForestGeo Studio" xmlns="http://www.topografix.com/GPX/1/1">'+body+'</gpx>';
}

// --- KML -> GeoJSON ---
function kmlToGeoJSON(text){
  var xml=new DOMParser().parseFromString(text,"application/xml");
  if(xml.getElementsByTagName("parsererror").length) throw new Error("KML解析エラー");
  function pc(str){ return str.trim().split(/\\s+/).map(function(t){ var p=t.split(","),c=[parseFloat(p[0]),parseFloat(p[1])]; if(p.length>2&&isFinite(parseFloat(p[2])))c.push(parseFloat(p[2])); return c; }).filter(function(c){return isFinite(c[0])&&isFinite(c[1]);}); }
  var feats=[],pms=xml.getElementsByTagName("Placemark");
  // Placemark から属性（name / description / ExtendedData）を収集する
  function _kmlText(parent,tag){ var e=parent.getElementsByTagName(tag)[0]; return e?e.textContent.trim():null; }
  function _kmlProps(pm){
    var props={};
    var nm=_kmlText(pm,"name"); if(nm) props.name=nm;
    var desc=_kmlText(pm,"description");
    if(desc) props.description=desc.replace(/<[^>]*>/g," ").replace(/\\s+/g," ").trim();
    var ed=pm.getElementsByTagName("ExtendedData")[0];
    if(ed){
      var ds=ed.getElementsByTagName("Data");           // <Data name="X"><value>V</value></Data>
      for(var x=0;x<ds.length;x++){ var k=ds[x].getAttribute("name"); if(!k) continue; var v=ds[x].getElementsByTagName("value")[0]; props[k]=v?v.textContent.trim():""; }
      var sd=ed.getElementsByTagName("SimpleData");      // <SimpleData name="X">V</SimpleData>
      for(var y=0;y<sd.length;y++){ var k2=sd[y].getAttribute("name"); if(!k2) continue; props[k2]=sd[y].textContent.trim(); }
    }
    return props;
  }
  for(var i=0;i<pms.length;i++){
    var pm=pms[i];
    var props=_kmlProps(pm);
    function add(geom){ if(geom) feats.push({type:"Feature",properties:props,geometry:geom}); }
    var P=pm.getElementsByTagName("Point");
    for(var a=0;a<P.length;a++){ var pcn=P[a].getElementsByTagName("coordinates")[0]; if(pcn){ var ar=pc(pcn.textContent); if(ar.length) add({type:"Point",coordinates:ar[0]}); } }
    var L=pm.getElementsByTagName("LineString");
    for(var b=0;b<L.length;b++){ var lcn=L[b].getElementsByTagName("coordinates")[0]; if(lcn){ var la=pc(lcn.textContent); if(la.length>=2) add({type:"LineString",coordinates:la}); } }
    var G=pm.getElementsByTagName("Polygon");
    for(var d=0;d<G.length;d++){
      var rings=[], ob=G[d].getElementsByTagName("outerBoundaryIs")[0];
      if(ob){ var oc=ob.getElementsByTagName("coordinates")[0]; if(oc) rings.push(pc(oc.textContent)); }
      var ibs=G[d].getElementsByTagName("innerBoundaryIs");
      for(var e2=0;e2<ibs.length;e2++){ var ic=ibs[e2].getElementsByTagName("coordinates")[0]; if(ic) rings.push(pc(ic.textContent)); }
      if(rings.length&&rings[0]&&rings[0].length>=3) add({type:"Polygon",coordinates:rings});
    }
  }
  return {type:"FeatureCollection",features:feats};
}

// --- GPX -> GeoJSON ---
function gpxToGeoJSON(text){
  var xml=new DOMParser().parseFromString(text,"application/xml");
  if(xml.getElementsByTagName("parsererror").length) throw new Error("GPX解析エラー");
  function pcoord(n){ var lat=parseFloat(n.getAttribute("lat")),lon=parseFloat(n.getAttribute("lon")); if(!isFinite(lat)||!isFinite(lon))return null; var e=n.getElementsByTagName("ele")[0],c=[lon,lat]; if(e&&isFinite(parseFloat(e.textContent)))c.push(parseFloat(e.textContent)); return c; }
  function seg(nodes){ var a=[]; for(var k=0;k<nodes.length;k++){ var c=pcoord(nodes[k]); if(c)a.push(c);} return a; }
  // 直下の子要素テキストのみ取得（trkpt の ele などを trk 属性に混ぜないため）
  function _childText(node,tag){ var ch=node.childNodes; for(var i=0;i<ch.length;i++){ if(ch[i].nodeType===1 && ch[i].nodeName.toLowerCase()===tag) return (ch[i].textContent||"").trim(); } return null; }
  function _gpxProps(node,tags){ var props={}; for(var i=0;i<tags.length;i++){ var v=_childText(node,tags[i]); if(v!=null && v!=="") props[tags[i]]=v; } return props; }
  var feats=[],W=xml.getElementsByTagName("wpt");
  for(var i=0;i<W.length;i++){ var c=pcoord(W[i]); if(c){ feats.push({type:"Feature",properties:_gpxProps(W[i],["name","desc","cmt","sym","type","ele","time"]),geometry:{type:"Point",coordinates:c}}); } }
  var T=xml.getElementsByTagName("trk");
  for(var t=0;t<T.length;t++){ var tp=_gpxProps(T[t],["name","desc","cmt","type","src"]); var S=T[t].getElementsByTagName("trkseg"); for(var s=0;s<S.length;s++){ var ar=seg(S[s].getElementsByTagName("trkpt")); if(ar.length>=2) feats.push({type:"Feature",properties:tp,geometry:{type:"LineString",coordinates:ar}}); } }
  var R=xml.getElementsByTagName("rte");
  for(var r=0;r<R.length;r++){ var rp=_gpxProps(R[r],["name","desc","cmt","type","src"]); var ar2=seg(R[r].getElementsByTagName("rtept")); if(ar2.length>=2) feats.push({type:"Feature",properties:rp,geometry:{type:"LineString",coordinates:ar2}}); }
  return {type:"FeatureCollection",features:feats};
}
""" if (opts["geojson_import"] or (opts["draw"] and opts["draw_export"])) else ""

        draw_export_js = """
// 作図を保存（保存ダイアログで GeoJSON / KML / GPX を選択）
async function exportDrawData() {
  if (!_drawFeatures || !_drawFeatures.length) { alert("保存する作図がありません。"); return; }
  const build = {
    geojson: function(){ return JSON.stringify({ type:"FeatureCollection", features:_drawFeatures }, null, 2); },
    kml:     function(){ return featuresToKML(_drawFeatures); },
    gpx:     function(){ return featuresToGPX(_drawFeatures); }
  };
  const mime = { geojson:"application/geo+json", kml:"application/vnd.google-earth.kml+xml", gpx:"application/gpx+xml" };
  // モダンブラウザ: ネイティブ保存ダイアログで拡張子（形式）を選択
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: "draw_features.geojson",
        types: [
          { description:"GeoJSON", accept:{ "application/geo+json":[".geojson",".json"] } },
          { description:"KML",     accept:{ "application/vnd.google-earth.kml+xml":[".kml"] } },
          { description:"GPX",     accept:{ "application/gpx+xml":[".gpx"] } }
        ]
      });
      const ext = (handle.name.split(".").pop() || "").toLowerCase();
      const fmt = ext==="kml" ? "kml" : ext==="gpx" ? "gpx" : "geojson";
      const w = await handle.createWritable();
      await w.write(new Blob([build[fmt]()], { type: mime[fmt] }));
      await w.close();
    } catch (err) { if (err && err.name === "AbortError") return; console.warn(err); alert("保存に失敗しました: " + (err && err.message ? err.message : err)); }
    return;
  }
  // フォールバック（保存ダイアログAPI非対応）: 形式を選んでダウンロード
  const pick = prompt("保存形式を番号で選択してください:\\n 1 = GeoJSON\\n 2 = KML\\n 3 = GPX", "1");
  if (pick === null) return;
  const fmt = pick.trim()==="2" ? "kml" : pick.trim()==="3" ? "gpx" : "geojson";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([build[fmt]()], { type: mime[fmt] }));
  a.download = "draw_features." + fmt;
  a.click();
  URL.revokeObjectURL(a.href);
}
""" if opts["draw"] and opts["draw_export"] else ""

        # --- GeoJSONインポート ---
        geojson_import_html = """
<button class="ctrl-btn" onclick="document.getElementById('_geojson-input').click()">📂 外部データを読込</button>
<input type="file" id="_geojson-input" accept=".geojson,.json,.kml,.gpx" style="display:none" onchange="importExternal(event)"/>
""" if opts["geojson_import"] else ""

        geojson_import_js = """
// ========== GeoJSONインポート ==========
let _importCounter = 0;

map.on("load", () => {
  // インポート用ソース・レイヤはファイルごとに動的追加
});

// ---- 外部データ読込レイヤの対話化（ポップアップ／属性集計／属性検索）----
// 取り込んだベクタは実体のあるベクタレイヤ（_import-*）として追加済みなので、
// 生成時には存在しなかったこれらレイヤを実行時に各機能へ登録する。
let _importPopup = null;
const _importPopupLayerIds = [];   // クリックでポップアップ対象とする _import-* レイヤID
let _importClickBound = false;

function _importBuildPopupHTML(props){
  const rows = Object.entries(props || {})
    .filter(([k]) => !String(k).startsWith("_"))
    .map(([key, value]) => `<tr><td class="pk">${key}</td><td>${value ?? ""}</td></tr>`)
    .join("");
  return rows ? `<table class="ptbl">${rows}</table>` : "<em>属性なし</em>";
}
function _importGetPopup(){
  // 既存の共有ポップアップがあれば再利用、無ければ専用ポップアップを生成
  if (typeof popup !== "undefined" && popup) return popup;
  if (!_importPopup) _importPopup = new maplibregl.Popup({ closeButton: true, maxWidth: "320px" });
  return _importPopup;
}
function _importBindPopup(ids){
  ids.forEach(function(layerId){
    if (_importPopupLayerIds.indexOf(layerId) < 0) _importPopupLayerIds.push(layerId);
    map.on("mouseenter", layerId, function(){ map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layerId, function(){ map.getCanvas().style.cursor = ""; });
  });
  if (_importClickBound) return;
  _importClickBound = true;
  // 1つのクリックハンドラで登録済み _import-* レイヤをまとめて判定する
  map.on("click", function(e){
    if (window.__statsActive) return;          // 属性集計モード中はポップアップ抑止
    const layers = _importPopupLayerIds.filter(function(id){ return map.getLayer(id); });
    if (!layers.length) return;
    const feats = map.queryRenderedFeatures(e.point, { layers: layers });
    if (!feats || !feats.length) return;
    _importGetPopup()
      .setLngLat(e.lngLat)
      .setHTML(_importBuildPopupHTML(feats[0].properties || {}))
      .addTo(map);
  });
}

// 取り込んだフィーチャ配列から、集計・検索に使える属性フィールド名を抽出
function _importCollectFields(feats){
  const seen = Object.create(null);
  const order = [];
  for (let i = 0; i < feats.length && i < 2000; i++){
    const p = feats[i] && feats[i].properties;
    if (!p) continue;
    for (const k in p){
      if (!Object.prototype.hasOwnProperty.call(p, k)) continue;
      if (String(k).charAt(0) === "_") continue;        // システム的フィールドは除外
      if (!seen[k]){ seen[k] = true; order.push(k); }
    }
  }
  return order;
}

// _import-* の3レイヤ（fill/line/circle）を各機能へ登録する
function _registerImportInteractivity(baseId, name, feats){
  const ids = [baseId+"-fill", baseId+"-line", baseId+"-circle"];
  // 1) 属性ポップアップ
  _importBindPopup(ids);
  // 2) 属性集計（stats-extension.js が有効なときのみ）
  if (window.__statsRegisterExternal) {
    try { window.__statsRegisterExternal(ids, name); } catch(e){ console.warn("[import] stats register failed", e); }
  }
  // 3) 属性検索（feature-search が有効なときのみ）
  if (window.__featureSearchRegister) {
    try {
      window.__featureSearchRegister({
        id: baseId, name: name, sourceId: baseId,
        fields: _importCollectFields(feats || []),
        features: feats || []
      });
    } catch(e){ console.warn("[import] search register failed", e); }
  }
}

function importExternal(event) {
  const file = event.target.files[0];
  if (!file) return;
  const _nm = (file.name || "").toLowerCase();
  const reader = new FileReader();
  reader.onload = function(e) {
    let data;
    try {
      if (_nm.endsWith(".kml")) data = kmlToGeoJSON(e.target.result);
      else if (_nm.endsWith(".gpx")) data = gpxToGeoJSON(e.target.result);
      else data = JSON.parse(e.target.result);
    } catch(err) { alert("ファイルの解析に失敗しました: " + (err && err.message ? err.message : err)); return; }
    const _hasFeat = data && ((data.features && data.features.length) || data.type === "Feature" || data.geometry || data.type === "GeometryCollection");
    if (!_hasFeat) { alert("読み込めるフィーチャがありませんでした。"); return; }
    const id = "_import-" + (++_importCounter);
    map.addSource(id, { type: "geojson", data: data });

    // ポリゴン塗りつぶし
    map.addLayer({ id: id + "-fill", type: "fill", source: id,
      filter: ["==", "$type", "Polygon"],
      paint: { "fill-color": "#ff3300", "fill-opacity": 0.3 } });
    // ライン（ポリゴンアウトライン含む）
    map.addLayer({ id: id + "-line", type: "line", source: id,
      filter: ["in", "$type", "Polygon", "LineString"],
      paint: { "line-color": "#ff3300", "line-width": 2 } });
    // ポイント
    map.addLayer({ id: id + "-circle", type: "circle", source: id,
      filter: ["==", "$type", "Point"],
      paint: { "circle-color": "#ff3300", "circle-radius": 6, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } });

    // レイヤパネルに追加
    if (typeof addToggle === "function") {
      addToggle([id+"-fill", id+"-line", id+"-circle"], "📂 " + file.name, "geojson", [], true);
    }

    // 取り込みデータをフィーチャ配列へ正規化（対話化登録・範囲フィットで共用）
    const _importFeats =
      data.type === "FeatureCollection" ? (data.features || []) :
      data.type === "Feature"           ? [data] :
      data.type === "GeometryCollection"? (data.geometries || []).map(g => ({ type:"Feature", geometry:g, properties:{} })) :
      (data.geometry || data.type)      ? [{ type:"Feature", geometry:(data.geometry || data), properties:(data.properties || {}) }] : [];

    // 属性ポップアップ・属性集計・属性検索に対応させる
    _registerImportInteractivity(id, file.name, _importFeats);

    // バウンディングボックスにフィット
    try {
      const coords = [];
      function collectCoords(geom) {
        if (!geom) return;
        if (geom.type === "Point") coords.push(geom.coordinates);
        else if (geom.type === "LineString" || geom.type === "MultiPoint") geom.coordinates.forEach(c => coords.push(c));
        else if (geom.type === "Polygon" || geom.type === "MultiLineString") geom.coordinates.forEach(r => r.forEach(c => coords.push(c)));
        else if (geom.type === "MultiPolygon") geom.coordinates.forEach(p => p.forEach(r => r.forEach(c => coords.push(c))));
        else if (geom.type === "GeometryCollection") geom.geometries.forEach(collectCoords);
      }
      _importFeats.forEach(f => collectCoords(f.geometry));
      if (coords.length) {
        const lngs = coords.map(c => c[0]), lats = coords.map(c => c[1]);
        map.fitBounds([[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]], { padding: 40 });
      }
    } catch(_) {}
  };
  reader.readAsText(file);
  event.target.value = "";  // 同じファイルを再度選択できるようにリセット
}
""" if opts["geojson_import"] else ""

        # --- 外部タイル入力（XYZ ラスタタイル）---
        # 閲覧者が {z}/{x}/{y} 形式のラスタタイルURLを入力して地図に重ねられる。
        # 表示ズーム範囲（minzoom/maxzoom）も任意で指定可能。
        external_tile_html = """
<button class="ctrl-btn" id="btn-exttile" onclick="toggleExtTilePanel()">🧩 外部タイルを追加</button>
<div id="exttile-panel" class="exttile-panel">
  <input id="exttile-url" type="text" placeholder="https://example.com/{z}/{x}/{y}.png" />
  <div class="exttile-row">
    <input id="exttile-name" type="text" placeholder="表示名（任意）" />
  </div>
  <div class="exttile-row">
    <label>最小Z<input id="exttile-minz" type="number" min="0" max="24" step="1" placeholder="0" /></label>
    <label>最大Z<input id="exttile-maxz" type="number" min="0" max="24" step="1" placeholder="22" /></label>
  </div>
  <div class="exttile-row exttile-actions">
    <button onclick="addExternalTile()">追加</button>
    <button class="exttile-cancel" onclick="toggleExtTilePanel()">閉じる</button>
  </div>
</div>
""" if opts.get("external_tile") else ""

        external_tile_js = """
// ========== 外部タイル入力（XYZ ラスタタイル）==========
let _extTileCounter = 0;

function toggleExtTilePanel(){
  const p = document.getElementById("exttile-panel");
  if(p) p.classList.toggle("open");
}

// ベースマップ直上（=データレイヤより下）へ挿入する位置を返す。
// 外部タイルは下地として扱い、QGIS由来のレイヤを隠さないようにする。
function _extTileBeforeId(){
  try {
    const layers = (map.getStyle().layers || []);
    for(const l of layers){
      if(l.id === "basemap" || l.id === "basemap2") continue;
      if(l.id.indexOf("_exttile-") === 0) continue;  // 既存の外部タイルより上
      return l.id;
    }
  } catch(e){}
  return undefined;
}

function addExternalTile(){
  const urlEl  = document.getElementById("exttile-url");
  const nameEl = document.getElementById("exttile-name");
  const minEl  = document.getElementById("exttile-minz");
  const maxEl  = document.getElementById("exttile-maxz");
  const url = (urlEl.value || "").trim();
  if(!url){ alert("タイルURLを入力してください。"); return; }
  if(url.indexOf("{z}") === -1 || url.indexOf("{x}") === -1 || url.indexOf("{y}") === -1){
    alert("URLには {z} / {x} / {y} を含めてください。\\n例: https://example.com/{z}/{x}/{y}.png");
    return;
  }
  let minz = parseInt(minEl.value, 10); if(!isFinite(minz)) minz = 0;
  let maxz = parseInt(maxEl.value, 10); if(!isFinite(maxz)) maxz = 22;
  minz = Math.max(0, Math.min(24, minz));
  maxz = Math.max(minz, Math.min(24, maxz));

  const id = "_exttile-" + (++_extTileCounter);
  const name = (nameEl.value || "").trim() || ("外部タイル " + _extTileCounter);

  try {
    map.addSource(id, {
      type: "raster",
      tiles: [url],
      tileSize: 256,
      minzoom: minz,
      maxzoom: maxz,
      attribution: ""
    });
    map.addLayer({
      id: id + "-layer",
      type: "raster",
      source: id,
      minzoom: minz,
      maxzoom: maxz,
      paint: { "raster-opacity": 1.0 }
    }, _extTileBeforeId());
  } catch(e){
    alert("タイルの追加に失敗しました: " + (e && e.message ? e.message : e));
    return;
  }

  // レイヤパネルへ登録（ON/OFF・透過率の調整が可能になる）
  if(typeof addToggle === "function"){
    addToggle([id + "-layer"], "🧩 " + name, "raster", [], true);
  }

  // 入力欄をリセットしてパネルを閉じる
  urlEl.value = ""; nameEl.value = ""; minEl.value = ""; maxEl.value = "";
  toggleExtTilePanel();
}
""" if opts.get("external_tile") else ""

        # ---- 気象アニメーション（降水量 + 風向風速）オプション ----
        # 重なり順: 気象は QGIS／外部データより最上位（=最後に addLayer）。
        # パネルでは最下段（パネル上段＝描画下層の慣例）。
        weather_script_tag = ""
        if opts.get("weather", False) and opt1_ok:
            _need_opt1("weather.js")
            weather_init_js = (
                "  // 気象レイヤ（降水レーダー + 風矢印）を追加（最上位レイヤ）\n"
                "  if (typeof WeatherExtension !== 'undefined') {\n"
                "    WeatherExtension.addToMap(map);\n"
                "  }\n"
            )
            _w_vis = "true" if opts.get("weather_vis", True) else "false"
            weather_panel_js = (
                "  if (typeof WeatherExtension !== 'undefined') {\n"
                "    WeatherExtension.addPanelToggle(map, '気象（降水量・風向風速）', %s);\n"
                "  }\n"
            ) % _w_vis

        # ---- 気象庁キキクル（危険度分布）オプション ----
        if opts.get("kikikuru", False) and opt1_ok:
            _need_opt1("kikikuru.js")
            kikikuru_init_js = (
                "  // キキクル（土砂・浸水・洪水の危険度分布）を追加（最上位レイヤ）\n"
                "  if (typeof KikikuruExtension !== 'undefined') {\n"
                "    KikikuruExtension.addToMap(map);\n"
                "  }\n"
            )
            _k_vis = "true" if opts.get("kikikuru_vis", True) else "false"
            kikikuru_panel_js = (
                "  if (typeof KikikuruExtension !== 'undefined') {\n"
                "    KikikuruExtension.addPanelToggle(map, '気象庁キキクル', %s);\n"
                "  }\n"
            ) % _k_vis

        # ---- CS立体図（標高タイル）オプション ----
        csmap_script_tag = ""
        if opts.get("csmap", False) and opt1_ok:
            _need_opt1("csmap-extension.js")
            micro_init["csmap"] = (
                "  // CS立体図レイヤを追加（微地形群）\n"
                "  if (typeof CsmapExtension !== 'undefined') {\n"
                "    CsmapExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("csmap_vis", True) else "false"
            micro_panel["csmap"] = (
                "  if (typeof CsmapExtension !== 'undefined') {\n"
                "    addToggle(['csmap1a-layer'], 'CS立体図（長野県林業総合センター）', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#89C3EB\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- 陰陽図（標高タイル）オプション ----
        inyouzu_script_tag = ""
        if opts.get("inyouzu", False) and opt1_ok:
            _need_opt1("inyouzu-extension.js")
            micro_init["inyouzu"] = (
                "  // 陰陽図レイヤを追加（微地形群）\n"
                "  if (typeof InyouzuExtension !== 'undefined') {\n"
                "    InyouzuExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("inyouzu_vis", True) else "false"
            micro_panel["inyouzu"] = (
                "  if (typeof InyouzuExtension !== 'undefined') {\n"
                "    addToggle(['inyouzu1a-layer'], '陰陽図（エアロトヨタ＊登録商標）', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#C4972F\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- MPI赤色立体地図（標高タイル）オプション ----
        mpirrim_script_tag = ""
        if opts.get("mpirrim", False) and opt1_ok:
            _need_opt1("mpirrim-extension.js")
            micro_init["mpirrim"] = (
                "  // MPI赤色立体地図レイヤを追加（微地形群）\n"
                "  if (typeof MpiRrimExtension !== 'undefined') {\n"
                "    MpiRrimExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("mpirrim_vis", True) else "false"
            micro_panel["mpirrim"] = (
                "  if (typeof MpiRrimExtension !== 'undefined') {\n"
                "    addToggle(['mpirrim1a-layer'], 'MPI赤色立体地図（アジア航測）', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#B7282D\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- CIマップ（標高タイル）オプション ----
        cimap_script_tag = ""
        if opts.get("cimap", False) and opt1_ok:
            _need_opt1("cimap-extension.js")
            micro_init["cimap"] = (
                "  // CIマップレイヤを追加（微地形群）\n"
                "  if (typeof CimapExtension !== 'undefined') {\n"
                "    CimapExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("cimap_vis", True) else "false"
            micro_panel["cimap"] = (
                "  if (typeof CimapExtension !== 'undefined') {\n"
                "    addToggle(['cimap1a-layer'], 'CIマップ（中央開発）', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#AFD147\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- 段彩陰影図（標高タイル）オプション ----
        colorrelief_script_tag = ""
        if opts.get("colorrelief", False) and opt1_ok:
            _need_opt1("colorrelief-extension.js")
            micro_init["colorrelief"] = (
                "  // 段彩陰影図を追加（微地形群・最下層）\n"
                "  if (typeof ColorreliefExtension !== 'undefined') {\n"
                "    ColorreliefExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("colorrelief_vis", True) else "false"
            micro_panel["colorrelief"] = (
                "  if (typeof ColorreliefExtension !== 'undefined') {\n"
                "    addToggle(['colorrelief1a-layer'], '段彩陰影図', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#BF7834\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- TOPEX （標高タイル・風向別）オプション ----
        if opts.get("topex", False) and opt1_ok:
            _need_opt1("topex-extension.js")
            micro_init["topex"] = (
                "  // TOPEX（風倒リスク）レイヤを追加（微地形群）\n"
                "  if (typeof TopexExtension !== 'undefined') {\n"
                "    TopexExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("topex_vis", True) else "false"
            micro_panel["topex"] = (
                "  if (typeof TopexExtension !== 'undefined') {\n"
                "    TopexExtension.addPanelToggle(map, 'TOPEX（風向別の地形露出指数）', %s);\n"
                "  }\n"
            ) % _vis

        # ---- TWI（標高タイル）オプション ----
        if opts.get("twi", False) and opt1_ok:
            _need_opt1("twi-extension.js")
            micro_init["twi"] = (
                "  // TWI傾斜量図を追加（微地形群）\n"
                "  if (typeof TwiExtension !== 'undefined') {\n"
                "    TwiExtension.addToMap(map);\n"
                "  }\n"
            )
            _vis = "true" if opts.get("twi_vis", True) else "false"
            micro_panel["twi"] = (
                "  if (typeof TwiExtension !== 'undefined') {\n"
                "    addToggle(['twi1a-layer'], 'TWI（地形湿潤指数）', 'raster',\n"
                "      [{\"label\":\"\",\"color\":\"#00CCCC\",\"shape\":\"fill\"}], %s);\n"
                "  }\n"
            ) % _vis

        # ---- Sentinel-2 変化解析（Earth Search STAC + COG）オプション ----
        # オプション1の保護JSとして読み込むが、WEB側UIはオプション2と同じく
        # 左コントロールのツールボタン + サブパネルで操作する。
        if opts.get("sentinel", False) and opt1_ok:
            _need_opt1("sentinel-change.js")
            sentinel_cfg = {
                "theme": {"main": theme["main"], "dark": theme["dark"]}
            }
            sentinel_init_js = (
                "\n  // Sentinel-2 変化解析（オプション1）: sentinel-change.js があれば有効化\n"
                "  if (typeof SentinelChangeExtension !== 'undefined') {\n"
                "    try { SentinelChangeExtension.enable(map, %s); }\n"
                "    catch(e){ console.warn('[sentinel] init failed', e); }\n"
                "  }\n"
            ) % json.dumps(sentinel_cfg, ensure_ascii=False)    

        # ---- 3Dビュー（国土地理院DEM5A）オプション ----
        terrain_source_js = ""
        terrain_btn_html = ""
        terrain_init_js = ""
        terrain_js = ""

        if opts.get("terrain_3d", False):
            # raster-dem ソースを既存ソースに追加（カンマ区切り）
            # タイルURLは addProtocol で登録した "gsidem://" を使い、
            # Canvas で R>=128（NA値・負の標高すべて）を (0,0,0)=0m に置換してから MapLibre に渡す。
            dem_source = """
      "terrain-dem": {
        "type": "raster-dem",
        "tiles": ["gsidem://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png"],
        "tileSize": 256,
        "encoding": "custom",
        "redFactor": 655.36,
        "greenFactor": 2.56,
        "blueFactor": 0.01,
        "baseShift": 0,
        "attribution": "国土地理院（DEM5A）"
      }"""
            # bm_source_js が空でなければカンマを付ける
            terrain_source_js = ("," if bm_source_js.strip() else "") + dem_source

            terrain_btn_html = '<button class="ctrl-btn" id="btn-3d" onclick="toggle3DView()">🏔️ 3Dビュー</button>'

            # map.on("load") 内で実行するスカイレイヤ追加コード
            terrain_init_js = """
  // 3D地形: スカイレイヤ追加（大気感の演出）
  if (!map.getLayer('sky')) {
    map.addLayer({
      id: 'sky',
      type: 'sky',
      paint: {
        'sky-type': 'atmosphere',
        'sky-atmosphere-sun': [0.0, 0.0],
        'sky-atmosphere-sun-intensity': 15
      }
    });
  }
"""
            # terrain_init_js は最後にまとめて load_js 先頭へ組み込む

            terrain_js = """
// ========== 3D地形ビュー（国土地理院 DEM5A）==========
//
// GSI DEM5A PNG 標高計算式:
//   x = R*65536 + G*256 + B
//   x < 2^23  → h = x * 0.01 m （正の標高 / 日本域: R ≦ 5 @ 富士山3776m）
//   x == 2^23 → NA  ← 無効値は (R=128,G=0,B=0) で表される
//   x > 2^23  → h = (x - 2^24) * 0.01 m （負の標高）
//
// 問題: MapLibre custom エンコーディングは線形計算のみ。
//   NA (128,0,0) → 655.36*128 = 83886m という超高値になり地形がスパイク状になる。
//
// 解決: addProtocol でタイルを Canvas 処理し、
//   R >= 128（NA値および負の標高）を (0,0,0) = 0m に置換してから MapLibre に渡す。
//   ※海岸線の遷移ピクセル（負の標高: R=129～255）も同様に超高値になるため一括処理。

(function registerGsiDemProtocol() {
  maplibregl.addProtocol('gsidem', async (params, abortController) => {
    // "gsidem://" を "https://" に置換して実際のURLを取得
    const url = params.url.replace('gsidem://', 'https://');
    let response;
    try {
      response = await fetch(url, { signal: abortController.signal });
    } catch (e) {
      // 取得失敗 → 全ピクセル 0m（黒タイル）として返す
      const blank = new Uint8Array(256 * 256 * 4); // 全ゼロ = 0m
      for (let i = 3; i < blank.length; i += 4) blank[i] = 255; // alpha=255
      return { data: blank.buffer };
    }
    if (!response.ok) {
      const blank = new Uint8Array(256 * 256 * 4);
      for (let i = 3; i < blank.length; i += 4) blank[i] = 255;
      return { data: blank.buffer };
    }

    const blob = await response.blob();
    const imgBitmap = await createImageBitmap(blob);

    const canvas = new OffscreenCanvas(imgBitmap.width, imgBitmap.height);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(imgBitmap, 0, 0);

    const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const d = imgData.data;

    // GSI DEM5A: x = R*65536 + G*256 + B
    //   x >= 2^23 (R >= 128) → NA または負の標高（海底・海面下）
    //   線形計算では R=129 → 84541m などの超高値になりスパイク発生するため
    //   R >= 128 のピクセルをすべて (0,0,0) = 0m（海面レベル）に置換する
    for (let i = 0; i < d.length; i += 4) {
      if (d[i] >= 128) {
        d[i] = 0; d[i + 1] = 0; d[i + 2] = 0;
      }
    }
    ctx.putImageData(imgData, 0, 0);

    // OffscreenCanvas → ArrayBuffer
    const resultBlob = await canvas.convertToBlob({ type: 'image/png' });
    const buffer = await resultBlob.arrayBuffer();
    return { data: buffer };
  });
})();

let _is3D = false;

function toggle3DView() {
  _is3D = !_is3D;
  const btn = document.getElementById('btn-3d');
  if (_is3D) {
    // 3Dモードへ
    map.setTerrain({ source: 'terrain-dem', exaggeration: 1.5 });
    map.easeTo({ pitch: 60, bearing: 30, duration: 1200 });
    if (btn) {
      btn.style.background = 'var(--green-dark)';
      btn.textContent = '🗺️ 2Dに戻す';
    }
  } else {
    // 2Dモードへ（真北・平面に戻す）
    map.setTerrain(null);
    map.easeTo({ pitch: 0, bearing: 0, duration: 1200 });
    if (btn) {
      btn.style.background = 'var(--green)';
      btn.textContent = '🏔️ 3Dビュー';
    }
  }
  // 単木SVGアイコン連携（treesvg.js があれば 3D/2D 切替に反応）
  if (window.__treeSvgOnViewModeChange) window.__treeSvgOnViewModeChange(_is3D);
}
"""

        # ---- スプリットビュー（レイヤ表示比較）オプション ----
        # 左: 起動時のレイヤ表示状態で固定したビュー（map2＝getStyleスナップショット）
        # 右: メインmap（レイヤパネルで表示を調整できる）
        # 中心位置・ズーム・方位・傾きは連動。分割境界はドラッグで左右に動かせる。
        split_btn_html = ""
        split_overlay_html = ""
        split_view_js = ""
        if opts.get("split_view"):
            split_btn_html = '<button class="ctrl-btn" id="btn-split" onclick="toggleSplitView()">🟰 スプリットビュー</button>'
            split_overlay_html = (
                '<div id="map2"></div>'
                '<div id="split-divider"><span class="split-handle">⇆</span></div>'
            )
            split_view_js = r"""
// ========== スプリットビュー（レイヤ表示比較） ==========
(function setupSplitView(){
  let map2 = null;
  let active = false;
  let ratio = 0.5;          // 分割位置（0..1, #map 左端からの割合）
  let _syncing = false;

  function el(id){ return document.getElementById(id); }

  function applyClip(){
    const wrap = el("map");
    if(!wrap || !map2) return;
    const w = wrap.clientWidth;
    const x = Math.round(w * ratio);
    // 左側に固定ビュー(map2)を表示。右側は下のメインmapが見える。
    const m2 = el("map2");
    if(m2) m2.style.clipPath = "inset(0 " + (w - x) + "px 0 0)";
    const dv = el("split-divider");
    if(dv) dv.style.left = x + "px";
  }

  function syncCamera(from, to){
    if(_syncing) return;
    _syncing = true;
    try {
      to.jumpTo({
        center: from.getCenter(),
        zoom: from.getZoom(),
        bearing: from.getBearing(),
        pitch: from.getPitch()
      });
    } catch(e){}
    _syncing = false;
  }

  function startDrag(){
    const wrap = el("map");
    function onMove(clientX){
      const r = wrap.getBoundingClientRect();
      let x = clientX - r.left;
      x = Math.max(40, Math.min(r.width - 40, x));  // 最小幅を確保
      ratio = x / r.width;
      applyClip();
    }
    function mm(e){ onMove(e.clientX); e.preventDefault(); }
    function tm(e){ if(e.touches && e.touches[0]) onMove(e.touches[0].clientX); }
    function up(){
      document.removeEventListener("mousemove", mm);
      document.removeEventListener("mouseup", up);
      document.removeEventListener("touchmove", tm);
      document.removeEventListener("touchend", up);
    }
    document.addEventListener("mousemove", mm);
    document.addEventListener("mouseup", up);
    document.addEventListener("touchmove", tm, {passive:true});
    document.addEventListener("touchend", up);
  }

  function buildMap2(){
    // 現在のレイヤ表示状態をそのままスナップショットとして複製
    let style;
    try { style = map.getStyle(); } catch(e){ style = null; }
    if(!style){ return null; }
    const m2 = new maplibregl.Map({
      container: "map2",
      style: style,
      center: map.getCenter(),
      zoom: map.getZoom(),
      bearing: map.getBearing(),
      pitch: map.getPitch(),
      attributionControl: false,
      interactive: false        // 入力はメインmapが受け、map2は連動表示のみ
    });
    m2.on("load", function(){
      // 3D地形のスナップショット（メインが3Dなら傾斜地形を合わせる）
      try {
        const t = (map.getTerrain && map.getTerrain());
        if(t) m2.setTerrain(t);
      } catch(e){}
      // FlatGeobuf ストリーミングを map2 にも適用（固定側もパン時にデータを表示）
      if(window.__fgbStreams){
        window.__fgbStreams.forEach(function(s){
          try { window.__streamFgbInto(m2, s.layerId, s.url); } catch(e){}
        });
      }
    });
    return m2;
  }

  function _onMainMove(){ if(map2) syncCamera(map, map2); }

  window.toggleSplitView = function(){
    const btn = el("btn-split");
    const m2el = el("map2");
    const dv = el("split-divider");
    if(!active){
      // 起動: map2 を現在の表示状態で生成（毎回スナップショットを取り直す）
      if(m2el) m2el.style.display = "block";
      if(dv) dv.style.display = "block";
      if(map2){ try { map2.remove(); } catch(e){} map2 = null; }
      map2 = buildMap2();
      if(!map2){
        if(m2el) m2el.style.display = "none";
        if(dv) dv.style.display = "none";
        alert("スプリットビューを開始できませんでした。");
        return;
      }
      active = true;
      ratio = 0.5;
      map.on("move", _onMainMove);   // カメラ連動（固定側は非対話なのでメインのみ監視）
      requestAnimationFrame(function(){ if(map2){ map2.resize(); } applyClip(); });
      if(btn){ btn.style.background = "var(--green-dark)"; btn.textContent = "🟰 スプリット解除"; }
    } else {
      // 解除
      active = false;
      map.off("move", _onMainMove);
      if(m2el){ m2el.style.display = "none"; m2el.style.clipPath = ""; }
      if(dv) dv.style.display = "none";
      if(map2){ try { map2.remove(); } catch(e){} map2 = null; }
      if(btn){ btn.style.background = "var(--green)"; btn.textContent = "🟰 スプリットビュー"; }
    }
  };

  function init(){
    const dv = el("split-divider");
    if(dv){
      dv.addEventListener("mousedown", function(e){ e.preventDefault(); startDrag(); });
      dv.addEventListener("touchstart", function(e){ startDrag(); }, {passive:true});
    }
    window.addEventListener("resize", function(){
      if(active && map2){ map2.resize(); applyClip(); }
    });
  }
  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
"""

        # ---- 単木SVGアイコン（オプション2 treesvg.js）連携 ----
        treesvg_script_tag = ""
        treesvg_js = ""
        if treesvg_layers and opt2_ok:
            # treesvg.js は認証付きローダー（protected_loader_js）で取得・注入する。
            _need_opt2("treesvg.js")

            treesvg_js = _TREESVG_JS_TEMPLATE \
                .replace("__TREE_LAYERS_JSON__", json.dumps(treesvg_layers, ensure_ascii=False)) \
                .replace("__TREE_SPECIES_JSON__", json.dumps(TREE_SVG_SPECIES, ensure_ascii=False)) \
                .replace("__TREE_TIERS_JSON__", json.dumps([[s, rep] for (s, rep, _lo) in TREE_SVG_TIERS], ensure_ascii=False)) \
                .replace("__TREE_ICON_PREFIX__", json.dumps(TREE_SVG_ICON_PREFIX)) \
                .replace("__TREE_ICON_IMAGE_EXPR__", json.dumps(self._tree_icon_image_expr(), ensure_ascii=False)) \
                .replace("__TREE_ICON_SIZE_EXPR__", json.dumps(self._tree_icon_size_expr(cy), ensure_ascii=False)) \
                .replace("__TREE_ICON_W_PX__", json.dumps(TREE_SVG_ICON_BASE_W_PX)) \
                .replace("__TREE_ICON_H_PX__", json.dumps(TREE_SVG_ICON_BASE_PX))

        # ---- 属性集計（統計）オプション（外部オプション stats-extension.js）----
        stats_script_tag = ""
        if opts.get("stats", False) and opt2_ok:
            stats_geojson_ids = [i for i in popup_layer_ids if isinstance(i, str)]
            stats_vt_ids = [i[0] for i in popup_layer_ids if isinstance(i, tuple)]
            stats_ids = stats_geojson_ids + stats_vt_ids
            # 外部データ読込が有効なら、生成時に集計対象レイヤが無くても
            # stats-extension を起動しておき、実行時に取り込んだベクタを後追い登録する。
            stats_allow_external = bool(opts.get("geojson_import", False))
            if stats_ids or stats_allow_external:
                # レイヤID → レイヤ名（パネル表示用）
                stats_name_map = {}
                for _ids, _nm, _knd, _lg, _vis in layer_id_groups:
                    for _lid in _ids:
                        stats_name_map[_lid] = _nm
                stats_cfg = {
                    "layers": stats_ids,
                    "layerNames": {i: stats_name_map.get(i, i) for i in stats_ids},
                    "theme": {"main": theme["main"], "dark": theme["dark"]},
                    "allowExternal": stats_allow_external,
                }
                # stats-extension.js は認証付きローダーで取得・注入する。
                _need_opt2("stats-extension.js")
                stats_init_js = (
                    "\n  // 属性集計（オプション2）: stats-extension.js があれば有効化\n"
                    "  if (typeof StatsExtension !== 'undefined') {\n"
                    "    try { StatsExtension.enable(map, %s); }\n"
                    "    catch(e){ console.warn('[stats] init failed', e); }\n"
                    "  }\n"
                ) % json.dumps(stats_cfg, ensure_ascii=False)
                # レイヤ追加後に実行されるよう load_js の末尾へ
                load_js = load_js + stats_init_js

        # ---- 印刷レイアウト（オプション2 print-extension.js）----
        if opts.get("print", False) and opt2_ok:
            print_legend = []
            for _ids, _nm, _knd, _lg, _vis in layer_id_groups:
                if _lg:  # 凡例アイテムを持つレイヤのみ
                    print_legend.append({
                        "name": _nm,
                        "items": _lg,               # [{"label","color","shape"}, ...]
                        "visible": bool(_vis),
                    })
            print_cfg = {
                "theme": {"main": theme["main"], "dark": theme["dark"], "text": theme["text"]},
                "source": source,                   # html の出典と合致させる
                "legend": print_legend,
            }
            _need_opt2("print-extension.js")
            print_init_js = (
                "\n  // 印刷レイアウト（オプション2）: print-extension.js があれば有効化\n"
                "  if (typeof PrintExtension !== 'undefined') {\n"
                "    try { PrintExtension.enable(map, %s); }\n"
                "    catch(e){ console.warn('[print] init failed', e); }\n"
                "  }\n"
            ) % json.dumps(print_cfg, ensure_ascii=False)
            load_js = load_js + print_init_js
            
        # ---- 森林ゾーニング（オプション2 shinrin-zoning-extension.js）----
        if opts.get("zoning", False) and opt2_ok:
            zoning_cfg = {
                "theme": {"main": theme["main"], "dark": theme["dark"], "text": theme["text"]},
                # 光田式 地位タイル、建物重心FGBと道路FGB
                "chiiBase":      "https://forestgeo.info/opendata/chii",
                "bldgBase":      "https://forestgeo.info/ForestGeoStudio/bldg",
                "roadTilesBase": "https://forestgeo.info/ForestGeoStudio/route",
                # 林野庁ベクトルタイル（地図に追加済みのものを参照）
                "meshSource":       "forest",
                "meshSourceLayer":  "全国森林資源メッシュ",
                "rinpanSource":      "shinrinkeikaku",
                "rinpanSourceLayer": "森林計画対象森林レイヤ",
            }
            _need_opt2("shinrin-zoning-extension.js")
            load_js = load_js + (
                "\n  if (typeof ShinrinZoningExtension !== 'undefined') {\n"
                "    try { ShinrinZoningExtension.enable(map, %s); }\n"
                "    catch(e){ console.warn('[zoning] init failed', e); }\n"
                "  }\n"
            ) % json.dumps(zoning_cfg, ensure_ascii=False)
            
        # ---- 林道線形シミュレーション（オプション2 roadsim-extension.js）----
        if opts.get("roadsim", False) and opt2_ok:
            roadsim_tree_layers = []
            for l in export_layers:
                st = l.get("style", {}) or {}
                if l.get("kind") == "vector-tile" and st.get("vt-geom-type") == "Point":
                    roadsim_tree_layers.append({"kind": "vector-tile",
                        "source": l.get("source") or l["id"],
                        "source_layer": st.get("vt-source-layer", "").strip()})
                elif l.get("kind") == "geojson":
                    roadsim_tree_layers.append({"kind": "geojson", "layerId": l["id"]})
            roadsim_cfg = {
                "theme": {"main": theme["main"], "dark": theme["dark"], "text": theme["text"]},
                "speciesField": TREE_SVG_FIELDS["species"],
                "heightField":  TREE_SVG_FIELDS["height"],
                "speciesOrder": [jp for (jp, _slug) in TREE_SVG_SPECIES],
                "treeLayers":   roadsim_tree_layers,
                "demSources": [
                    {"id":"dem1a","tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png","maxz":18},
                    {"id":"dem5a","tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png","maxz":15},
                    {"id":"dem",  "tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",  "maxz":14},
                ],
            }
            _need_opt2("roadsim-extension.js")
            load_js = load_js + (
                "\n  if (typeof RoadSimExtension !== 'undefined') {\n"
                "    try { RoadSimExtension.enable(map, %s); }\n"
                "    catch(e){ console.warn('[roadsim] init failed', e); }\n"
                "  }\n"
            ) % json.dumps(roadsim_cfg, ensure_ascii=False)

        # ---- 架線集材シミュレーション（オプション2 cablesim-extension.js）----
        if opts.get("cablesim", False) and opt2_ok:
            cablesim_tree_layers = []
            for l in export_layers:
                st = l.get("style", {}) or {}
                if l.get("kind") == "vector-tile" and st.get("vt-geom-type") == "Point":
                    cablesim_tree_layers.append({"kind": "vector-tile",
                        "source": l.get("source") or l["id"],
                        "source_layer": st.get("vt-source-layer", "").strip()})
                elif l.get("kind") == "geojson":
                    cablesim_tree_layers.append({"kind": "geojson", "layerId": l["id"]})
            cablesim_cfg = {
                "theme": {"main": theme["main"], "dark": theme["dark"], "text": theme["text"]},
                "speciesField": TREE_SVG_FIELDS["species"],
                "heightField":  TREE_SVG_FIELDS["height"],
                "volumeField":  "材積",
                "dbhField":     "胸高直径",
                "speciesOrder": [jp for (jp, _slug) in TREE_SVG_SPECIES],
                "treeLayers":   cablesim_tree_layers,
                "drawSourceId": "_draw-src",
                "demSources": [
                    {"id":"dem1a","tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png","maxz":18},
                    {"id":"dem5a","tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png","maxz":15},
                    {"id":"dem",  "tpl":"https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",  "maxz":14},
                ],
            }
            _need_opt2("cablesim-extension.js")
            load_js = load_js + (
                "\n  if (typeof CableSimExtension !== 'undefined') {\n"
                "    try { CableSimExtension.enable(map, %s); }\n"
                "    catch(e){ console.warn('[cablesim] init failed', e); }\n"
                "  }\n"
            ) % json.dumps(cablesim_cfg, ensure_ascii=False)

        # ---- ルート検索（オプション2 route-extension.js）----
        if opts.get("route", False) and opt2_ok:
          avoid_layers = []
          for l in export_layers:
              # 読み込んだGeoJSONレイヤのうち、ポリゴンを含むものを通行不能候補に
              if l.get("kind") == "geojson":
                data = l.get("data", {}) or {}
                feats = data.get("features", []) if isinstance(data, dict) else []
                has_poly = any(
                    (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")
                    for f in feats
                )
                if has_poly:
                    avoid_layers.append({
                        "kind": "geojson",
                        "source": l["id"],
                        "layerId": l["id"],
                    })
              # ベクトルタイルのポリゴンレイヤも通行不能候補に
              elif l.get("kind") == "vector-tile":
                  st = l.get("style", {}) or {}
                  if st.get("vt-geom-type") == "Polygon":
                      avoid_layers.append({
                          "kind": "vector-tile",
                          "source": l.get("source") or l["id"],
                          "sourceLayer": st.get("vt-source-layer", "").strip(),
                      })
          
          route_cfg = {
              # 配信URL（タイルとmanifestの置き場所）。変更時はここを書き換える。
              "tilesBase": "https://forestgeo.info/ForestGeoStudio/route",
              # コリドーの最大タイル枚数。起点・終点が遠すぎたら拒否する安全装置。
              # 〜50km目安なら 49 で足りる。
              "maxTiles": 49,
              # 起点・終点を最近傍道路ノードへスナップする最大距離(m)
              # 林道網はエッジが長く、道路上をクリックしても最近傍 *ノード* が
              # km単位で遠いことが普通にあるため、1.5〜2km は確保する。
              "snapMeters": 2000,
              # コリドーbbox の膨らまし量(m)。経路が直線でない分の安全側マージン。
              "corridorBufferM": 2000,
              # 重みフィールド名（manifestと一致させる）
              "weightFields": {"distance": "length_m", "time": "travel_time_s"},
              # 配色テーマ（roadsimと同じ流儀）
              "theme": {"main": theme["main"], "dark": theme["dark"], "text": theme["text"]},
              # 描画ツール(ForestGeo Studio本体)のGeoJSON source id。
              # JS側で "_draw-src" を含む候補リストを自動探索するため None でも動くが、
              # 明示しておくほうが堅実。
              "drawSourceId": "_draw-src",
              # 明示的に通行不能として使うレイヤ群
              "avoidLayerIds": avoid_layers,
          }
          _need_opt2("route-extension.js")
          load_js = load_js + (
              "\n  if (typeof RouteExtension !== 'undefined') {\n"
              "    try { RouteExtension.enable(map, %s); }\n"
              "    catch(e){ console.warn('[route] init failed', e); }\n"
              "  }\n"
          ) % json.dumps(route_cfg, ensure_ascii=False)

        # ===== レイヤ重なり順／パネル表示順の最終組み立て =====
        micro_order = ["colorrelief", "twi", "topex", "cimap", "inyouzu", "mpirrim", "csmap"]
        micro_init_js  = "".join(micro_init.get(k, "")  for k in micro_order)
        micro_panel_js = "".join(micro_panel.get(k, "") for k in micro_order)
        # この時点の load_js は QGIS レイヤ＋オプション2 操作系を含む。
        # 先頭へ 3D sky → 微地形群、末尾へ 気象 を連結し最終的な描画順を確定する。
        load_js = terrain_init_js + micro_init_js + sentinel_init_js + load_js + kikikuru_init_js + weather_init_js

        # パネル: 上段＝描画下層。ベースマップ → 微地形 → QGIS → 気象 の順に addToggle。
        for _bm_id, _bm_name, _bm_vis in basemap_panel_entries:
            panel_basemap_js += (
                "  if (map.getLayer(%s)) addToggle([%s], %s, 'raster', [], %s);\n"
                % (json.dumps(_bm_id), json.dumps(_bm_id),
                   json.dumps(_bm_name, ensure_ascii=False),
                   "true" if _bm_vis else "false")
            )
        panel_js = panel_basemap_js + micro_panel_js + panel_qgis_js + kikikuru_panel_js + weather_panel_js

        # ===== 属性検索バー（ベクタレイヤの属性値で地物検索）=====
        feature_search_html = ""
        feature_search_js = ""
        if opts.get("feature_search", True):
            fs_layers = []
            for _l in export_layers:
                if _l.get("kind") == "fgb":
                    fs_layers.append({
                        "id": _l["id"],
                        "name": _l["name"],
                        "url": _l["url"],
                        "geom": _l.get("geom", "Point"),
                        "fields": _l.get("fields", []),
                    })
            # 外部データ読込が有効なら、fgb レイヤが無くても検索バーを出しておき、
            # 実行時に取り込んだベクタを後追い登録できるようにする。
            fs_allow_external = bool(opts.get("geojson_import", False))
            if fs_layers or fs_allow_external:
                feature_search_html = (
                    '<div id="feature-search-bar">'
                    '<span class="fs-label">🔍 属性検索</span>'
                    '<select id="fs-layer" title="検索対象レイヤ"></select>'
                    '<select id="fs-field" title="検索する属性フィールド"></select>'
                    '<input id="fs-input" type="text" placeholder="属性値（部分一致）" />'
                    '<button id="fs-go" onclick="runFeatureSearch()">検索</button>'
                    '<button id="fs-clear" onclick="clearFeatureSearch()">クリア</button>'
                    '<span id="fs-status"></span>'
                    '</div>'
                )
                feature_search_js = _FEATURE_SEARCH_JS_TEMPLATE.replace(
                    "__FS_LAYERS__", json.dumps(fs_layers, ensure_ascii=False)
                )

        # ---- 保護JSローダー（認証付き fetch → <script>注入）----
        protected_specs = []
        for fn in needed_opt1:
            protected_specs.append({"opt": 1, "file": fn, "token": token_opt1})
        for fn in needed_opt2:
            protected_specs.append({"opt": 2, "file": fn, "token": token_opt2})

        turf_head_js = ('<script src="https://unpkg.com/@turf/turf@7/turf.min.js"></script>'
                if ((opts.get("roadsim", False) or opts.get("route", False) or opts.get("cablesim", False)) and opt2_ok) else "")

        protected_loader_js = ""
        if protected_specs:
            protected_loader_js = (
                "<script>\n"
                "(function(){\n"
                "  var BASE=" + json.dumps(server_base) + ";\n"
                "  var SPECS=" + json.dumps(protected_specs, ensure_ascii=False) + ";\n"
                "  function loadOne(s){\n"
                "    var url=BASE+'/js.php?opt='+s.opt+'&file='+encodeURIComponent(s.file);\n"
                "    return fetch(url,{headers:{'Authorization':'Bearer '+s.token}})\n"
                "      .then(function(r){ if(!r.ok){throw new Error(s.file+' '+r.status);} return r.text(); })\n"
                "      .then(function(code){\n"
                "        var el=document.createElement('script');\n"
                "        el.textContent=code;\n"
                "        document.head.appendChild(el);\n"  # 同期実行＝グローバル展開
                "        return true;\n"
                "      })\n"
                "      .catch(function(e){ console.warn('[option] load failed:', e); return false; });\n"
                "  }\n"
                "  window.__optReady = Promise.all(SPECS.map(loadOne));\n"
                "})();\n"
                "</script>"
            )

        layer_panel_html = '<div id="layer-panel"><h2>レイヤ</h2><div id="layer-list"></div></div>' if opts["layer_panel"] else ""
        coord_cell = '<div class="status-cell" id="coord-cell">マウス座標</div>' if opts["coord"] else ""
        # 自動生成した出典（ベースマップ＋データレイヤ）。手入力の出典とは別欄に表示。
        basemap_cell_html = (
            f'<div class="status-cell" id="basemap-cell" title="自動生成された出典">{auto_source}</div>'
            if auto_source else ""
        )

        # 各種機能ボタンをグループ化（レイヤパネルON/OFFは常駐、住所検索以下をまとめる）
        grouped_buttons = [
            addr_html, gps_html, share_html, area_calc_html, dist_calc_html,
            draw_html, draw_export_html, geojson_import_html, external_tile_html,
            terrain_btn_html, split_btn_html,
        ]
        tools_present = any(s.strip() for s in grouped_buttons)
        tool_group_inner = "\n        ".join(s for s in grouped_buttons if s.strip())
        tools_toggle_html = (
            '<button class="ctrl-btn" id="btn-tools" onclick="toggleToolGroup()">基本機能パネルON/OFF</button>'
            if tools_present else ""
        )

        return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<link href="https://unpkg.com/maplibre-gl@4.1.3/dist/maplibre-gl.css" rel="stylesheet"/>
<script src="https://unpkg.com/maplibre-gl@4.1.3/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/flatgeobuf@3.34.0/dist/flatgeobuf-geojson.min.js"></script>
{turf_head_js}
<style>
:root {{
  --green: {theme["main"]};
  --green-dark: {theme["dark"]};
  --text: {theme["text"]};
  --panel-bg: rgba(255,255,255,0.97);
  --shadow: 0 2px 10px rgba(0,0,0,0.3);
  --r: 4px;
  --bar-h: 53px;
  --font: "Noto Sans JP","Meiryo",sans-serif;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;font-family:var(--font);font-size:13px}}
#app{{display:flex;flex-direction:column;height:100vh;overflow:hidden}}
#title-bar{{min-height:var(--bar-h);background:var(--green);color:var(--text);display:flex;align-items:center;gap:12px;padding:6px 14px;font-size:15px;font-weight:700;flex-shrink:0;box-shadow:var(--shadow);z-index:10}}
#title-text{{flex:1 1 auto;min-width:0;white-space:normal;overflow-wrap:anywhere;line-height:1.25}}
#title-logo{{height:45px;width:auto;flex-shrink:0;margin-left:auto;display:block}}
#main{{display:flex;flex:1;overflow:hidden;position:relative}}
#map{{flex:1;position:relative}}
#map2{{position:absolute;inset:0;display:none;z-index:1;pointer-events:none}}
#split-divider{{position:absolute;top:0;bottom:0;left:50%;width:6px;margin-left:-3px;background:var(--green);display:none;z-index:22;cursor:ew-resize;box-shadow:0 0 6px rgba(0,0,0,.5)}}
#split-divider .split-handle{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:26px;height:26px;border-radius:50%;background:var(--green);color:var(--text);display:flex;align-items:center;justify-content:center;font-size:13px;box-shadow:0 0 6px rgba(0,0,0,.5);user-select:none;pointer-events:none}}
#left-controls{{position:absolute;top:10px;left:10px;z-index:20;display:flex;flex-direction:column;gap:4px}}
.ctrl-btn{{background:var(--green);color:var(--text);border:none;border-radius:var(--r);padding:6px 14px;font-size:13px;font-family:var(--font);cursor:pointer;white-space:nowrap;box-shadow:var(--shadow);min-width:120px;text-align:left}}
.ctrl-btn:hover{{background:var(--green-dark)}}
#btn-layer-panel{{font-size:11px;padding:4px 8px;min-width:auto;opacity:0.75;letter-spacing:0.02em}}
#btn-layer-panel:hover{{opacity:0.75}}
#btn-tools{{font-size:11px;padding:5px 10px}}
#tool-group{{display:flex;flex-direction:column;gap:4px}}
#tool-group.collapsed{{display:none}}
.addr-panel{{display:none;position:absolute;top:10px;left:134px;z-index:30;background:var(--panel-bg);border-radius:var(--r);box-shadow:var(--shadow);padding:6px;width:270px;flex-direction:column;gap:4px}}
.addr-panel.open{{display:flex}}
.addr-row{{display:flex;gap:4px}}
.addr-panel input{{flex:1;border:1px solid #ccd;border-radius:var(--r);padding:4px 8px;font-size:13px;font-family:var(--font)}}
.addr-panel button{{background:var(--green);color:#fff;border:none;border-radius:var(--r);padding:4px 10px;cursor:pointer;font-size:13px}}
.addr-results{{list-style:none;margin:4px 0 0;padding:0;max-height:220px;overflow-y:auto}}
.addr-results li{{padding:5px 8px;font-size:13px;cursor:pointer;border-radius:var(--r);color:#222}}
.addr-results li:hover{{background:rgba(0,0,0,.07)}}
.addr-results li.addr-msg{{cursor:default;color:#888}}
.exttile-panel{{display:none;position:absolute;top:10px;left:134px;z-index:30;background:var(--panel-bg);border-radius:var(--r);box-shadow:var(--shadow);padding:8px;width:280px;flex-direction:column;gap:6px;color:#222}}
.exttile-panel.open{{display:flex}}
.exttile-panel input{{border:1px solid #ccd;border-radius:var(--r);padding:4px 8px;font-size:12px;font-family:var(--font);width:100%}}
.exttile-panel .exttile-row{{display:flex;gap:8px;align-items:center}}
.exttile-panel .exttile-row label{{flex:1;font-size:11px;display:flex;align-items:center;gap:4px;color:#222}}
.exttile-panel .exttile-row label input{{width:60px}}
.exttile-panel .exttile-actions{{justify-content:flex-end;gap:6px}}
.exttile-panel button{{background:var(--green);color:var(--text);border:none;border-radius:var(--r);padding:4px 12px;cursor:pointer;font-size:12px}}
.exttile-panel button.exttile-cancel{{background:#999}}
#layer-panel{{width:220px;background:var(--green);color:var(--text);flex-shrink:0;display:flex;flex-direction:column;overflow-y:auto;box-shadow:-2px 0 8px rgba(0,0,0,.15);z-index:5}}
#layer-panel h2{{font-size:13px;font-weight:700;padding:10px 12px 6px;border-bottom:1px solid rgba(255,255,255,.25);background:var(--green-dark)}}
.layer-item{{display:flex;align-items:center;gap:8px;padding:7px 12px;border-bottom:1px solid rgba(255,255,255,.1);font-size:12px;line-height:1.3;cursor:pointer}}
.layer-item:hover{{background:rgba(255,255,255,.12)}}
.layer-item input[type=checkbox]{{accent-color:#fff;width:14px;height:14px;cursor:pointer}}
.layer-opacity-btn{{background:transparent;border:none;color:var(--text);font-size:13px;cursor:pointer;padding:0 2px;opacity:.75;line-height:1}}
.layer-opacity-btn:hover{{opacity:1}}
.layer-opacity{{display:none;align-items:center;gap:8px;padding:4px 12px 8px 34px;border-bottom:1px solid rgba(255,255,255,.1)}}
.layer-opacity input[type=range]{{flex:1;accent-color:#fff;cursor:pointer}}
.layer-opacity .layer-opacity-val{{font-size:11px;min-width:34px;text-align:right;opacity:.9}}
.layer-legend{{padding:2px 12px 6px 34px;border-bottom:1px solid rgba(255,255,255,.1)}}
.legend-item{{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:11px;line-height:1.4;color:var(--text)}}
.legend-swatch{{flex-shrink:0;width:16px;height:16px;border-radius:2px;border:1px solid rgba(0,0,0,.2)}}
.legend-swatch.line{{height:4px;border-radius:2px;border:none;margin-top:6px}}
.legend-swatch.circle{{border-radius:50%}}
.legend-label{{opacity:.9}}
#feature-search-bar{{background:var(--green-dark);color:var(--text);display:flex;align-items:center;gap:6px;padding:5px 8px;flex-shrink:0;flex-wrap:wrap;z-index:10;font-size:12px;border-top:1px solid rgba(255,255,255,.18)}}
#feature-search-bar .fs-label{{font-weight:700;white-space:nowrap}}
#feature-search-bar select,#feature-search-bar input{{border:1px solid #ccd;border-radius:var(--r);padding:3px 6px;font-size:12px;font-family:var(--font);background:#fff;color:#222}}
#feature-search-bar #fs-input{{flex:1;min-width:140px}}
#feature-search-bar button{{background:var(--green);color:var(--text);border:none;border-radius:var(--r);padding:3px 12px;cursor:pointer;font-size:12px;white-space:nowrap}}
#feature-search-bar button:hover{{background:var(--green)}}
#feature-search-bar #fs-status{{opacity:.9;white-space:nowrap}}
#status-bar{{min-height:30px;background:var(--green);display:flex;align-items:center;padding:4px 6px;flex-shrink:0;gap:6px;z-index:10}}
.status-cell{{background:var(--green-dark);color:var(--text);border-radius:var(--r);padding:3px 12px;font-size:11px;white-space:nowrap}}
#source-group{{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}}
#meter-group{{flex-shrink:0;display:flex;flex-direction:column;gap:3px}}
#attrib-cell{{text-align:left;white-space:normal;overflow-wrap:anywhere;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.3}}
#basemap-cell{{text-align:left;white-space:normal;overflow-wrap:anywhere;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.35;font-size:10.5px;opacity:.92}}
#zoom-cell{{min-width:200px;text-align:right}}
#coord-cell{{min-width:200px;text-align:right}}
#credit-bar{{height:24px;background:var(--green-dark);color:var(--text);display:flex;align-items:center;justify-content:flex-end;padding:0 10px;font-size:11px;flex-shrink:0;z-index:10}}
.maplibregl-popup-content{{font-family:var(--font);font-size:12px;padding:8px 12px;border-radius:4px;max-width:300px;max-height:240px;overflow-y:auto}}
.ptbl{{border-collapse:collapse;width:100%}}
.ptbl td{{padding:2px 4px;border-bottom:1px solid #eee}}
.pk{{color:#1e5c2e;font-weight:700;white-space:nowrap}}
.maplibregl-ctrl-top-right{{top:10px;right:10px}}

@media (max-width: 768px){{

  #main{{
    flex-direction: column;
  }}

  #layer-panel{{
    position: absolute;
    right: -240px;
    top: 0;
    height: 100%;
    transition: right 0.3s;
  }}

  #layer-panel.open{{
    right: 0;
  }}

  #coord-cell{{
    display: none;
  }}

  #zoom-cell{{
    min-width: auto;
  }}

  .ctrl-btn{{
    min-width: auto;
    padding: 6px 10px;
  }}
}}

</style>
<link rel="stylesheet" href="MaptureField/photo-extension.css" onerror="this.remove()"/>
<script>
(function(){{
  var s=document.createElement('script');
  s.src='MaptureField/photo-extension.js';
  document.head.appendChild(s);
}})();
</script>
{protected_loader_js}
</head>
<body>
<div id="app">
  <div id="title-bar"><span id="title-text">{title}</span><img id="title-logo" src="https://forestgeo.info/ForestGeoStudio/header.png" alt="ForestGeo Studio" onerror="this.style.display='none'"></div>
  <div id="main">
    <div id="map">
      <div id="left-controls">
       <button class="ctrl-btn" id="btn-layer-panel" onclick="toggleLayerPanel()">レイヤパネルON/OFF</button>
        {tools_toggle_html}
        <div id="tool-group">
        {tool_group_inner}
        </div>
      </div>
      {split_overlay_html}
    </div>
    {layer_panel_html}
  </div>
  {feature_search_html}
  <div id="status-bar">
    <div class="status-cell" id="scale-cell"></div>
    <div id="source-group">
      <div class="status-cell" id="attrib-cell">{source or "出典"}</div>
      {basemap_cell_html}
    </div>
    <div id="meter-group">
      <div class="status-cell" id="zoom-cell">ズームレベル：--</div>
      {coord_cell}
    </div>
  </div>
  <div id="credit-bar">Powered by&nbsp;
   <a href="https://github.com/naokimuroki" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: underline;">
   ForestGeo Studio</a>, &nbsp;
   <a href="https://forestgeo.info/" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: underline;">
   Forestgeo.info</a>, &nbsp;
   <a href="https://maplibre.org/" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: underline;">
   MapLibre GL JS</a>
  </div>
</div>

<script>
const map = new maplibregl.Map({{
  container: "map",
  style: {{
    version: 8,
    glyphs: "https://fonts.openmaptiles.org/{{fontstack}}/{{range}}.pbf",
    sources: {{ {bm_source_js}{terrain_source_js} }},
    layers: [ {bm_layer_js} ]
  }},
  center: [{cx:.6f}, {cy:.6f}],
  zoom: 10,
  maxZoom: 20,
  attributionControl: false
}});

const sharedView = {shared_view_expr};
if(sharedView) {{
  map.jumpTo(sharedView);
}}

{zoom_ctrl_js}
{scale_js}

// レイヤ種別に応じた *-opacity 系プロパティをまとめて設定する（透過率スライダー用）
function setLayerOpacity(layerIds, v){{
  layerIds.forEach(id => {{
    const ly = map.getLayer(id);
    if(!ly) return;
    try {{
      switch(ly.type){{
        case "raster": map.setPaintProperty(id, "raster-opacity", v); break;
        case "fill": map.setPaintProperty(id, "fill-opacity", v); break;
        case "line": map.setPaintProperty(id, "line-opacity", v); break;
        case "circle":
          map.setPaintProperty(id, "circle-opacity", v);
          map.setPaintProperty(id, "circle-stroke-opacity", v);
          break;
        case "symbol":
          map.setPaintProperty(id, "icon-opacity", v);
          map.setPaintProperty(id, "text-opacity", v);
          break;
        case "fill-extrusion": map.setPaintProperty(id, "fill-extrusion-opacity", v); break;
      }}
    }} catch(e) {{ /* データ駆動式などで設定不可なら無視 */ }}
  }});
}}

function addToggle(ids, name, kind, legend, initialVisible){{
  const list = document.getElementById("layer-list");
  if(!list) return;
  const layerIds = Array.isArray(ids) ? ids : [ids];
  // initialVisible が明示的に false の場合は非表示スタート
  const startVisible = (initialVisible !== false);
  let inlineSwatchAdded = false;

  // --- チェックボックス行 ---
  const label = document.createElement("label");
  label.className = "layer-item";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = startVisible;
  // 初期非表示の場合はマップレイヤも非表示に設定
  if (!startVisible) {{
    layerIds.forEach(id => {{
      if(map.getLayer(id)) map.setLayoutProperty(id, "visibility", "none");
    }});
  }}
  checkbox.onchange = () => {{
    const vis = checkbox.checked ? "visible" : "none";
    layerIds.forEach(id => {{
      if(map.getLayer(id)) map.setLayoutProperty(id, "visibility", vis);
    }});
    if(legendDiv) legendDiv.style.display = checkbox.checked ? "" : "none";
    // 単木SVGアイコン: 3D表示中の ON/OFF を symbol レイヤにも反映させる
    if(window.__treeSvgReapply) window.__treeSvgReapply();
  }};
  const span = document.createElement("span");
  span.textContent = name;
  label.appendChild(checkbox);
  label.appendChild(span);
  list.appendChild(label);

  // --- 凡例行（色分けルールがある or 単色でも shape を表示） ---
  let legendDiv = null;
  if(legend && legend.length > 0) {{
    // 単色・ラベルなしの場合は小さいスウォッチのみ（行内に折りたたまない）
    const isSingle = legend.length === 1 && !legend[0].label;
    if(isSingle) {{
      // チェックボックス行の右側に小スウォッチを埋め込む
      const sw = document.createElement("span");
      sw.className = "legend-swatch " + (legend[0].shape || "fill");
      sw.style.background = legend[0].color;
      sw.style.marginLeft = "auto";
      sw.style.width = "12px";
      sw.style.height = legend[0].shape === "line" ? "3px" : "12px";
      label.appendChild(sw);
      inlineSwatchAdded = true;
    }} else {{
      // 複数ルール → 凡例ブロック
      legendDiv = document.createElement("div");
      legendDiv.className = "layer-legend";
      legend.forEach(item => {{
        const row = document.createElement("div");
        row.className = "legend-item";
        const sw = document.createElement("span");
        sw.className = "legend-swatch " + (item.shape || "fill");
        sw.style.background = item.color;
        const lbl = document.createElement("span");
        lbl.className = "legend-label";
        lbl.textContent = item.label;
        row.appendChild(sw);
        row.appendChild(lbl);
        legendDiv.appendChild(row);
      }});
      list.appendChild(legendDiv);
    }}
  }}

  // --- 透過率（不透明度）ボタン＋スライダー ---
  // 各レイヤ行に「◐」ボタンを置き、押すと下にスライダーが開く。
  const opacBtn = document.createElement("button");
  opacBtn.type = "button";
  opacBtn.className = "layer-opacity-btn";
  opacBtn.textContent = "◐";
  opacBtn.title = "透過率を調整";
  opacBtn.style.marginLeft = inlineSwatchAdded ? "6px" : "auto";
  // ボタンのクリックがチェックボックス(label)のトグルを誘発しないよう抑止
  opacBtn.addEventListener("click", (ev) => {{
    ev.preventDefault();
    ev.stopPropagation();
    opacRow.style.display = (opacRow.style.display === "none") ? "flex" : "none";
  }});
  label.appendChild(opacBtn);

  const opacRow = document.createElement("div");
  opacRow.className = "layer-opacity";
  opacRow.style.display = "none";
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0"; slider.max = "100"; slider.value = "100";
  const valLbl = document.createElement("span");
  valLbl.className = "layer-opacity-val";
  valLbl.textContent = "100%";
  slider.addEventListener("input", () => {{
    const v = Number(slider.value) / 100;
    valLbl.textContent = slider.value + "%";
    setLayerOpacity(layerIds, v);
  }});
  opacRow.appendChild(slider);
  opacRow.appendChild(valLbl);
  list.appendChild(opacRow);

  // 地図共有リンクで表示設定を復元できるよう、トグル情報を登録順に保持する
  (window.__toggles = window.__toggles || []).push({{ layerIds: layerIds, checkbox: checkbox, slider: slider }});
}}

function toggleLayerPanel(){{
  const panel = document.getElementById("layer-panel");
  if(!panel) return;
  panel.classList.toggle("open");
}}

function toggleToolGroup(){{
  const g = document.getElementById("tool-group");
  if(!g) return;
  g.classList.toggle("collapsed");
}}

// ---------------------------------------------------------------
// FlatGeobuf ストリーミングローダー
// bbox（現在の表示範囲）に絞って fgb をフェッチし GeoJSON ソースを更新する。
// HTTP/HTTPS サーバー上のファイルでは byte-range リクエストが有効になり
// 表示範囲外のフィーチャーは転送されない。
// ---------------------------------------------------------------
async function loadFgbLayer(map, layerId, url, styleLayers, initialVisible) {{
  // 空の GeoJSON ソースを登録
  map.addSource(layerId, {{
    type: "geojson",
    data: {{ type: "FeatureCollection", features: [] }}
  }});

  // スタイルレイヤを追加
  styleLayers.forEach(ld => map.addLayer(ld));

  // 初期表示設定
  if (initialVisible === false) {{
    styleLayers.forEach(ld => {{
      if (map.getLayer(ld.id)) map.setLayoutProperty(ld.id, "visibility", "none");
    }});
  }}

  let _loading = false;

  async function fetchAndUpdate() {{
    if (_loading) return;
    _loading = true;
    try {{
      const bounds = map.getBounds();
      const rect = {{
        minX: bounds.getWest(),
        minY: bounds.getSouth(),
        maxX: bounds.getEast(),
        maxY: bounds.getNorth()
      }};
      const features = [];
      // flatgeobuf.deserialize は bbox を渡すと byte-range で部分取得する
      for await (const f of flatgeobuf.deserialize(url, rect)) {{
        features.push(f);
      }}
      const src = map.getSource(layerId);
      if (src) src.setData({{ type: "FeatureCollection", features }});
    }} catch(e) {{
      console.warn("[fgb] fetch error:", layerId, e);
    }} finally {{
      _loading = false;
    }}
  }}

  await fetchAndUpdate();
  map.on("moveend", fetchAndUpdate);

  // スプリットビュー等で別マップにも同じデータを流せるよう設定を控えておく
  (window.__fgbStreams = window.__fgbStreams || []).push({{ layerId: layerId, url: url }});
}}

// 既存のソース（複製スタイル等で既に登録済み）に対し、bbox 絞り込みの
// FlatGeobuf ストリーミングだけを後付けする。スプリットビューの固定側マップ用。
window.__streamFgbInto = function(targetMap, layerId, url) {{
  let _loading = false;
  async function fetchAndUpdate() {{
    if (_loading) return;
    _loading = true;
    try {{
      const b = targetMap.getBounds();
      const rect = {{ minX: b.getWest(), minY: b.getSouth(), maxX: b.getEast(), maxY: b.getNorth() }};
      const features = [];
      for await (const f of flatgeobuf.deserialize(url, rect)) {{ features.push(f); }}
      const src = targetMap.getSource(layerId);
      if (src) src.setData({{ type: "FeatureCollection", features }});
    }} catch(e) {{ /* 固定側の取得失敗は無視 */ }}
    finally {{ _loading = false; }}
  }}
  fetchAndUpdate();
  targetMap.on("moveend", fetchAndUpdate);
}};

// 操作系レイヤ（描画・面積・距離・路網）を最前面へ。
// 微地形/段彩などの保護JSラスタが後から追加され上に被さるのを防ぐ。
function _raiseOverlays() {{
  try {{
    var prefixes = ["_area-", "_dist-", "_draw-", "_import-", "roadsim-", "cablesim-"];
    (map.getStyle().layers || []).forEach(function (l) {{
      for (var i = 0; i < prefixes.length; i++) {{
        if (l.id.indexOf(prefixes[i]) === 0) {{ try {{ map.moveLayer(l.id); }} catch (e) {{}} break; }}
      }}
    }});
  }} catch (e) {{}}
}}

map.on("load", async () => {{
  window.forestGeoStudioMap = map;
  window.qgis2maplibreProMap = map; // 後方互換（旧名称）
  window.qgis2maplibreMap = map;    // 後方互換（旧名称）
  // 保護JS（オプション）の読込完了を待ってから初期化する
  if(window.__optReady) {{ try {{ await window.__optReady; }} catch(e) {{}} }}
  if(!sharedView) {{
    map.fitBounds([[{xmin:.6f},{ymin:.6f}],[{xmax:.6f},{ymax:.6f}]]);
  }}
{load_js}
{panel_js}
  // 地図共有リンクで指定されたレイヤ表示設定（ON/OFF・不透明度）を復元
  if(window.applySharedLayerState) window.applySharedLayerState();
  // 単木SVGアイコン: ソース追加後・保護JS読込後に初期化
  if(window.__treeSvgInit) window.__treeSvgInit();
  // 描画・計測・路網などの操作系レイヤを最前面へ（微地形/段彩ラスタに隠れないように）
  _raiseOverlays();
  map.once("idle", _raiseOverlays);
}});

{popup_js}
{coord_js}
{zoom_js}
{addr_js}
{gps_js}
{share_js}
{area_calc_js}
{dist_calc_js}
{draw_js}
{geoio_js}
{draw_export_js}
{geojson_import_js}
{external_tile_js}
{terrain_js}
{split_view_js}
{treesvg_js}
{feature_search_js}
window.addEventListener("load", () => {{
  if(window.innerWidth < 768){{
    const panel = document.getElementById("layer-panel");
    if(panel) panel.classList.remove("open");
    const g = document.getElementById("tool-group");
    if(g) g.classList.add("collapsed");
  }}
}});

</script>
</body>
</html>
"""
