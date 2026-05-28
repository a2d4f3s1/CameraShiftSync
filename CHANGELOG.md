# Changelog

Camera Shift Sync の変更履歴です。

書式は [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) に倣い、バージョン番号は [Semantic Versioning](https://semver.org/) に従います。

## [Unreleased]

### Added

- AddonPreferences の `Category (N-Panel)` フィールドに、デフォルト値 `CameraShift` へ戻すリセットボタン（`LOOP_BACK` アイコン）を追加

## [0.2.0] - 2026-05-27

機能 A end-to-end 実装。ワールド固定の D 平面ターゲットを軸にカメラを動かすと `shift_x` / `shift_y` が自動連動し、画角中心が常にターゲットに乗り続ける「構図保持移動」を N-Panel から提供する。透視カメラ限定（`Camera.type == 'PERSP'`）。

### Added

- N-Panel `Camera Shift Sync` タブを新設。Initialize / Camera / Plate Transform / Lens Shift / Plate の各セクションを配置
- D 平面の Initialize / De-Initialize:
  - Initialize チェックボックスで `target_distance` から T を派生計算し、Initialize 時のカメラ位置 / 回転 / lens / shift_x/y / plate 中心の 5 値を snapshot
  - De-Initialize は明示トグル、外部編集（Properties Editor 等での camera transform 編集）、active 解除、save、`cam.type` を PERSP 以外に変更、のいずれでも自動発火（Bake 確定: cam は現状維持、Initialize 状態と UI Delta / snapshot / show_plate のみリセット）
  - 保存時はサンドイッチ方式で `.blend` には Not Initialized で書き、セッション継続中はシームレスに復元
- Camera Position 操作（プロパティ駆動）:
  - Delta X / Y / Z: D 平面ローカル各軸の純粋並進
  - Radial Distance: d-cam 直線上の前進・後退
  - 両者は P からの双方向 back-sync
- Plate Transform 操作（プロパティ駆動）:
  - Location: D 平面ローカル軸の delta、カメラ剛体追従で `shift_x/y` 不変
  - Rotation: snapshot 派生の baked rotation 上の delta、単軸・多軸を問わず `shift_x/y` 不変
- 自動連動（`bpy.msgbus.subscribe_rna`）:
  - `cam.shift_x` / `cam.shift_y` 編集で T を新しい shift view direction に移動（現在の perpendicular depth 保持）
  - `cam.lens` / `cam.angle` / `cam.angle_x` / `cam.angle_y` 編集で `shift_x/y` を再計算して画角中心を T に維持
  - `cam.type` 変更（PERSP → ORTHO/PANO 等）で Initialize 状態を自動 De-Init
- Get Distance from Click オペレーター: 3D ビューポート上でクリックした位置の perpendicular depth を `target_distance` に書き込む（target_distance スライダー編集と等価）
- D 平面プレート overlay 描画:
  - GPU 描画ハンドラで半透明 fill + edge を描画
  - In Front 切替（depth test の有無）
  - fill color / edge color / edge width をカメラごとにカスタマイズ可能
  - AddonPreferences の Plate Defaults から Reset Plate ボタンで初期値復元
- AddonPreferences の General セクション:
  - N-Panel タブカテゴリ `Category (N-Panel)` を AddonPreferences から動的解決（変更時はパネル再登録）
- `tools/verify_shift_unit.py`: shift 逆算 round-trip / sensor_fit dispatch のヘッドレス検証スクリプト
- `tools/dev.ps1` / `tools/build_release.ps1`: 開発インフラ（Junction deploy / ヘッドレス register 検査 / リリース ZIP 生成）

### Changed

- 機能 A の仕様を「D 平面ワールド固定 + shift 連動 + プロパティ駆動」に確定（`docs/spec.md` 全面書き直し）
- `core.py` の shift 逆算式を `Camera.view_frame()` ベースに確定（`depth = abs(view_frame()[0].z) = lens / fac`、`shift_x = (V_local.x / -V_local.z) * depth`）。`sensor_fit` の dispatch（HORIZONTAL / VERTICAL / AUTO × render aspect × portrait sensor）はアドオン側で再実装せず Blender に委譲
- 機能 A の対象を透視カメラ限定（`Camera.type == 'PERSP'`）に整理。オルソカメラは shift がカメラ X/Y 並行移動と等価で機能 A の意義が薄れるため対象外
- カメラ操作の主インターフェースを Operator（モーダル）からプロパティ駆動の N-Panel スライダーに変更（補助操作のみオペレーター）
- `properties.py`: カメラごとの状態を `base_distance` / `plane_origin` / `plane_normal` / `plane_up` から、Initialize snapshot 5 値 + UI Delta + 派生量設計に再構成
- `plate_baked_rotation` を Initialize 時の `init_cam_rotation` snapshot から都度導出する派生計算に変更（source 一元化）

### Fixed

- `cam.lens` / FOV 編集時の shift 自動連動で、カメラビューにおいて lens 反映と shift 反映の 1 frame 差により構図中心が D 平面から外れて戻る「ガタガタ」を解消。msgbus owner を shift 用と other 用に分離し、lens callback 内で shift owner を transient clear → 即時書き込み → re-subscribe するパターンで deferred 発火を drop
- Plate Transform Rotation 編集時、多軸同時編集で Euler の非可換性により剛体追従の invariant がずれる問題を `new_world_rot @ old_world_rot.inverted()` の式に書き換えて解消
- Get Distance from Click でクリックすると Initialize 状態が無条件にリセットされていた問題を、target_distance 編集と等価な挙動に変更して解消
- Get Distance from Click のカーソル形状が N-Panel ウィジェット hover で戻る問題を `MOUSEMOVE` 時の cursor 再アサートで緩和（Blender 仕様上完全解消は不可）

### Removed

- AddonPreferences のショートカット関連プロパティと keymap 自動登録機構を一旦保留（UI 実装完成後に再考）
- 未使用関数 `orbit_camera_position` / `radial_camera_position` を削除（球面オービット案を D 平面平行並進に切り替えたあとの残骸）

## [0.1.0] - 2026-05-18

初回タグ。骨組みのみ。**インストールしても機能は動作しません。**

### Added

- Blender 4.2 Extensions マニフェスト（`blender_manifest.toml`）
- AddonPreferences の General セクション
  - N-Panel カテゴリ設定（`Category (N-Panel)`、デフォルト `CameraShift`）
  - Camera Shift Move 用ショートカット編集 UI（修飾キー + 主要キー、デフォルト `Shift+G`）
- Camera ごとの設定プロパティ（`base_distance` / `show_plate`）
- `CAMERA_OT_shift_move` モーダルオペレーターの骨格（実行ロジックは未実装）
- 3D Viewport N-Panel 骨格（`Camera Shift Sync` タブ）
- 数学関数のシグネチャ定義（`core.py`）
- 仕様書 `docs/spec.md`（機能 A 確定 / 機能 B 未確定）

### Known Limitations

- すべてのオペレーターは未実装。発火しても `CANCELLED` を返す
- 機能 B（レンズシフト ⇄ 回転）の仕様は未確定
