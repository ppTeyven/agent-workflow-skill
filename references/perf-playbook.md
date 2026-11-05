# 性能排查手册

> 面向 WebGL / three.js / 图形与前端项目。
> 通用原则：**先量化定位，再一次改一个变量，再复测同一指标。**

---

## 0. 标准探针组合（一次跑完，别分多轮）

```javascript
// A. 主线程阻塞区间
new PerformanceObserver(l => {
  for (const e of l.getEntries()) longs.push({ t: e.startTime, d: e.duration });
}).observe({ entryTypes: ['longtask'] });

// B. CPU 热点：CDP Profiler.start / Profiler.stop，self time 排序
// C. GPU 同步点：给 WebGL*.prototype 打桩计时
//    linkProgram / getProgramParameter / getActiveUniform
//    getUniformLocation / getShaderParameter / readPixels / finish
// D. 资源流向
performance.getEntriesByType('resource')
```

**判据**：CPU self time 第一名占比 > 50% → 唯一根因，先修它，别看别的。

**GPU 专项判别**：

| 现象 | 结论 |
|---|---|
| `linkProgram` 累计 ≈ 0，但某个 `getProgramParameter` 占大头 | **不是 GPU 慢，是查询触发同步等待** → 见 §1 |
| `linkProgram` 本身就慢 | GPU 真在编译 → 减变体 / 降 shader 复杂度 |

---

## 1. 同步 GPU 查询（隐藏的性能杀手）

### 症状

首帧卡住数秒，交互恢复后一切正常。CPU profile 里 `getProgramParameter` /
`getProgramInfoLog` / `checkShaderErrors` 占了绝大部分时间。

### 机理

这些 API 要求 program **已链接完成**。某些驱动 / 实现下会阻塞 CPU 直到 GPU 编译完管线。
危险在于 uniform 表往往在 program 创建时**立刻**查询，于是 N 个 shader 变体变成
「建一个 → 等 GPU → 查 → 再建下一个」的**串行**流水线。

> 实测案例：这类查询把主线程锁死 **19.4 秒**。

### 首选修法：uniform 表惰性建立

把构造器里的查询拆出，改成第一次真正访问 `seq` / `map` / `setValue` 时才查：

```js
class WebGLUniforms {
  constructor(gl, program) {
    this.__gl = gl; this.__prog = program;
    this.__init = false; this.__seq__ = []; this.__map__ = {};
  }
  __ensure() {
    if (this.__init) return;
    this.__init = true;              // ← 必须先置位，否则自递归
    const n = this.__gl.getProgramParameter(this.__prog, this.__gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; ++i) {
      const info = this.__gl.getActiveUniform(this.__prog, i);
      const loc = this.__gl.getUniformLocation(this.__prog, info.name);
      addUniform(info, loc, this);
    }
  }
  get seq() { this.__ensure(); return this.__seq__; }
  get map() { this.__ensure(); return this.__map__; }
}
```

**递归陷阱**：建表过程会回写 `this.seq` / `this.map`。用 getter 时必须先置 `__init = true` 再建表。

**语义安全**：`ACTIVE_UNIFORMS` 的结果只取决于 program 是否已链接，与查询时刻无关。

### 陷阱：`compileAsync` 不等于并行编译

```js
this.compileAsync = function (scene, camera) {
  const materials = this.compile(scene, camera);   // ← 同步！阻塞就在这里
  return new Promise(resolve => { /* 轮询 isReady() */ });
};
```

调用 `compileAsync` 但**不 await 其 promise**，等于没开。

### 叠加：门控首帧

```js
renderer.compileAsync(scene, camera).then(() => { gate = false; });
// gate 期间渲染循环跳过真正的渲染
```

### 验收标准

修复后目标 API 的累计耗时应从**秒级降到接近 0**，不是"变少一点"。

---

## 2. 帧率采样不能用加载期数据标定

### 症状

优化完了，帧率反而永久锁在极低值（如 1 FPS），怎么调参数都不动。

### 真实原因

自适应节流器在**启动加载期**标定了帧率上限。加载期本来就有长任务，
p95 帧间隔被拉到 1003ms → 上限被锁到 850ms/帧 → 之后永远 1 FPS。
上限是永久的，不会因为加载结束自动恢复。

### 正确做法

- 标定只在"场景已就绪 + 已渲染若干稳定帧"之后开始。
- 加载期帧数据**单独统计**，不参与上限计算。
- 上限要有"可恢复"机制：连续 N 秒稳定高于上限时允许上调。

```js
const CALIBRATE_AFTER_READY = true;   // 就绪前不采样
const RECOVER_ENABLED = true;         // 稳定后允许上调上限
```

---

## 3. 强度为 0 的灯仍被编进 shader

### 症状

像素着色开销随灯数量线性增长，但画面上看不出区别。

### 真实原因

强度为 0 的灯照样进了 shader，每像素都跑一遍**零贡献**的光照计算。

### 正确做法

剔除掉：`light.visible = false`（或从 lights 数组移除），让着色器变体不包含它。

**验收方式**：像素输出必须**完全不变**（不是"差不多"）。用同一视角截图逐像素比对，
或跑一次数值 diff，差异必须为 0。

---

## 4. 延迟初始化：IIFE 被 TypeError 静默打断

### 症状

某个模块"从来就没跑成功过"。加了日志也不显示 —— 因为日志代码本身在被打断的 IIFE 里。

### 真实原因

初始化代码写成了**脚本加载时同步执行**：

```js
// 错：加载时场景还没建好 → TypeError，整个 IIFE 挂掉
const scene = getScene();
setupLighting(scene);   // ← 抛错，后面的代码全部不执行
```

一个 `TypeError` 会让整个 IIFE 静默终止，看起来像"功能没实现"。

### 正确做法

挂到"就绪后再启动"的钩子：

```js
// 对：等就绪信号
whenReady(() => {
  const scene = getScene();
  setupLighting(scene);
});
```

并且给 IIFE 加**错误可见性**：

```js
try { /* ... */ } catch (e) { console.error('[init] failed', e); window.__initError = String(e); }
```

这样"没生效"和"报错了"能被区分开。

---

## 5. 改压缩/构建产物

GLB、大纹理、bundle：

1. **不要用文本工具打开。** 用脚本切片、按需加载、剥掉未使用的嵌图。
2. 改之前**先量化体积收益**（省多少字节 / 多少 draw call），不值得就别改。
3. **锚点定位 + 精确字符串替换**，禁止整体格式化重排。锚点必须唯一。
4. 改完**立刻语法校验**：

   ```
   node scripts/check_syntax.mjs <file.html>      # 校验内联 <script type="module">
   node --check <file.js>
   python -m py_compile <file.py>
   ```

5. **三层对账**：磁盘字节 → HTTP 响应字节 → 运行时内存态。
6. 静态服务器必须发 `Cache-Control: no-store`，否则会看到"改动回退"的假象。

---

## 6. 抓真实帧

`preserveDrawingBuffer: false` 时浏览器截屏拿到空帧。
用 `scripts/capture_frame.py`（本地接收端）或在同一次 JS 调用里同步渲染 + `toDataURL`。
详见 `visual-verification.md` 第 2~3 节。

---

## 7. 优化验收清单

```
[ ] 根因有实测证据（探针数据），不是推测
[ ] 一次只改一个变量，改后复测同一指标
[ ] 给出「修复前 → 修复后」对照值
[ ] 视觉类任务：已渲染出真实画面并亲眼看过，按看图清单逐条确认
[ ] 画质 / 行为影响为 0（逐像素 diff = 0），或已明确说明差异
[ ] 语法校验通过
[ ] 三层对账通过
[ ] 临时脚本已清理
```

---

## 8. 常见误判

| 现象 | 错误结论 | 正确结论 |
|---|---|---|
| `linkProgram` 耗时长 | GPU 编译慢 | 看 `getProgramParameter` 是不是更大头 —— 那是同步等待不是编译 |
| `compileAsync` 调用了 | 已经并行编译 | 忘了 `await` promise，或 `compile()` 本身是同步的 |
| 帧率上不去 | 场景太重 | 先看加载期标定锁死了上限 |
| 加了灯变卡 | 光照计算太重 | 零强度灯没剔除，shader 变体里还在 |
| 改完没生效 | 缓存 | 改的可能被覆盖 —— A/B 对比验证 |
