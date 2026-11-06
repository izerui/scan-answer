"""可行性验证：无定位块的背面，用大外框定位 + 纠偏。

步骤：
1. 二值化 → 找面积最大的若干个四边形轮廓（作文大外框）→ 取角点
2. 用真实背面图纠偏后作为“模板”
3. 对模板施加随机旋转/平移/缩放（模拟进纸歪斜），再用同样方法找外框，
   求单应矩阵对齐回模板，统计对齐误差（mm）
用法: python back_align.py <背面图.png> <输出目录>
"""
import sys
from pathlib import Path

import cv2
import numpy as np

DPI = 200
MM = DPI / 25.4


def find_frames(gray, k=2):
    """返回面积最大的 k 个四边形外框，每个为 4x2 角点（按 左上、右上、右下、左下 排序），整体按 x 排序。"""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 51, 15)
    # 闭运算接上虚断的细线
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gray.shape
    quads = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 0.08 * w * h:  # 外框至少占全页 8%
            continue
        approx = cv2.approxPolyDP(c, 0.01 * cv2.arcLength(c, True), True)
        if len(approx) != 4:
            continue
        quads.append((area, order(approx.reshape(4, 2).astype(np.float32))))
    # 同一个框会有内外两条轮廓，按中心去重保留外侧
    quads.sort(key=lambda t: -t[0])
    picked = []
    for area, q in quads:
        cen = q.mean(0)
        if all(np.linalg.norm(cen - p.mean(0)) > 50 * MM for p in picked):
            picked.append(q)
        if len(picked) == k:
            break
    return sorted(picked, key=lambda q: q[:, 0].min())


def order(pts):
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def skew_deg(q):
    (x0, y0), (x1, y1) = q[0], q[1]
    return np.degrees(np.arctan2(y1 - y0, x1 - x0))


def main(src, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    gray = cv2.imread(src, cv2.IMREAD_GRAYSCALE)
    frames = find_frames(gray)
    print(f"原始背面：找到外框 {len(frames)} 个，倾斜角 " + ", ".join(f"{skew_deg(q):+.2f}°" for q in frames))

    # 1) 纠偏得到模板：用两个外框 8 个角点拟合“轴对齐”的目标位置
    src_pts = np.vstack(frames)
    ang = np.mean([skew_deg(q) for q in frames])
    M = cv2.getRotationMatrix2D((gray.shape[1] / 2, gray.shape[0] / 2), ang, 1.0)
    tpl = cv2.warpAffine(gray, M, (gray.shape[1], gray.shape[0]), borderValue=255)
    tpl_frames = find_frames(tpl)
    tpl_pts = np.vstack(tpl_frames)
    print(f"纠偏后模板：倾斜角 " + ", ".join(f"{skew_deg(q):+.2f}°" for q in tpl_frames))
    cv2.imwrite(str(out / "back-template.png"), tpl)

    # 2) 随机扰动 → 重新定位 → 对齐误差
    rng = np.random.default_rng(42)
    h, w = tpl.shape
    errs = []
    for i in range(20):
        a = rng.uniform(-3, 3)
        s = rng.uniform(0.98, 1.02)
        tx, ty = rng.uniform(-8, 8, 2) * MM
        P = cv2.getRotationMatrix2D((w / 2, h / 2), a, s)
        P[:, 2] += (tx, ty)
        img = cv2.warpAffine(tpl, P, (w, h), borderValue=255)
        img = cv2.add(img, rng.normal(0, 6, img.shape).astype(np.int16).clip(0, 255).astype(np.uint8) // 4)
        fr = find_frames(img)
        if len(fr) != 2:
            print(f"  #{i}: 外框只找到 {len(fr)} 个 ✗")
            errs.append(np.inf)
            continue
        H, _ = cv2.findHomography(np.vstack(fr), tpl_pts)
        # 误差：取模板上一组网格检查点，正向扰动后再用 H 映射回来
        gx, gy = np.meshgrid(np.linspace(0.1 * w, 0.9 * w, 9), np.linspace(0.1 * h, 0.9 * h, 7))
        chk = np.stack([gx.ravel(), gy.ravel()], 1).astype(np.float32)
        moved = cv2.transform(chk[None], P)[0]
        back = cv2.perspectiveTransform(moved[None], H)[0]
        e = np.linalg.norm(back - chk, axis=1).max() / MM
        errs.append(e)
        if i == 0:
            aligned = cv2.warpPerspective(img, H, (w, h), borderValue=255)
            cv2.imwrite(str(out / "perturbed-0.png"), img)
            cv2.imwrite(str(out / "aligned-0.png"), aligned)
            diff = cv2.absdiff(aligned, tpl)
            cv2.imwrite(str(out / "diff-0.png"), 255 - diff)
        print(f"  #{i:02d}: 旋转 {a:+.2f}° 缩放 {s:.3f} 平移 ({tx / MM:+.1f},{ty / MM:+.1f})mm → 最大对齐误差 {e:.3f} mm")
    ok = [e for e in errs if np.isfinite(e)]
    print(f"成功 {len(ok)}/20，最大误差 {max(ok):.3f} mm，平均 {np.mean(ok):.3f} mm")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
