# Camera Shift Sync

Blender 4.2 以降向けの透視カメラアドオン。ワールド固定の D 平面を不変ターゲットとして、カメラを動かしながら `shift_x` / `shift_y` を自動連動させ、画角中心を常にターゲットに乗せ続ける「構図保持移動」を N-Panel から提供します。

## 特徴

- **D 平面ターゲット**: 任意の被写体面に Initialize すると、その平面の中心 T を世界座標で固定したまま、カメラだけを動かす操作モードに入ります
- **shift 自動連動**: カメラ位置 / 回転 / 焦点距離（lens）/ FOV を変えても `shift_x` / `shift_y` がリアルタイムで再計算され、画角中心が T からずれません
- **Camera Position 操作**:
  - **Delta X / Y / Z**: D 平面ローカル各軸の純粋並進（軌跡が直線）
  - **Radial Distance**: D 平面中心と一直線上の前進・後退
  - 両者は双方向で同期する 2 つのビュー
- **Plate Transform 操作**: D 平面そのものの位置 / 回転を編集するとカメラが剛体追従し、`shift_x/y` を維持したまま全体を運搬・回転できます
- **自動 De-Initialize**: 外部からカメラ transform を編集 / カメラを非アクティブに / `.blend` 保存 / カメラ type を ORTHO・PANO に切り替え、のいずれかで Initialize 状態が自動解除されます（カメラ自身の状態はそのまま残るので「ベイク確定」として使えます）
- **D 平面プレート overlay**: 3D ビューポートに半透明プレートを描画して位置を可視化（色 / 太さ / depth test ON/OFF をカメラごとに設定可能）

## 対応バージョン

- Blender **4.2** 以降（Extensions 形式）
- 透視カメラ（`Camera.type == 'PERSP'`）のみ対象。オルソ / パノラマカメラは UI がグレーアウトします

## インストール

1. リリースから `CameraShiftSync-X.Y.Z.zip` をダウンロード
2. Blender を起動し、`Edit > Preferences > Add-ons > Install from Disk...` から ZIP を選択
3. 有効化（チェックボックス）
4. 3D ビューポートの N-Panel に `Camera Shift Sync` タブが追加されます（タブ名は AddonPreferences の `Category (N-Panel)` で変更可能）

## 基本操作

1. **カメラを選択し、N-Panel の Camera Shift Sync タブを開く**
2. **Target Distance** に被写体までの距離を入力するか、**Get Distance from Click** ボタンでビューポート上の被写体をクリック
3. **Initialize** チェックボックスを ON にすると、その位置に D 平面が固定され、Camera Position / Plate Transform セクションが開きます
4. Camera Position の **Delta X / Y / Z** / **Radial Distance** を動かすとカメラが動き、`shift_x/y` が自動で追従します
5. Plate Transform の **Location** / **Rotation** を動かすと、カメラごと全体が並進・回転します（構図維持）
6. Initialize を OFF にする、または外部からカメラを動かすと自動 De-Initialize（カメラの現状の位置・shift・lens はそのまま残ります）

## ライセンス

GPL-3.0-or-later。詳細は `LICENSE` を参照してください（含まれていない場合は [SPDX:GPL-3.0-or-later](https://spdx.org/licenses/GPL-3.0-or-later.html) の本文に準じます）。

## 変更履歴

[CHANGELOG.md](CHANGELOG.md) を参照してください。
