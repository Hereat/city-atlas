/**
 * city-atlas 的 Worker：只在静态资源没命中时才跑。
 *
 * Workers 静态资源的默认次序是「先 assets，没命中才进这段代码」（`run_worker_first`
 * 默认 false，配置里没有打开它）。所以正常的分片与地图包请求一个字节都不经过这里，
 * 这段代码看到的全是 404 —— 整个设计立在这一条上，改配置时别把它弄反。
 *
 * **这段代码答出去的 404 会被写进用户的库。** App 把主源的 404 当成「这一格没有城」
 * 并落成终态（`CityStore.settle`），而终态要等下一代目录才会重算。所以在记账之前
 * 先自证一句：自家 `directory/latest.json` 还在不在？不在就说 503（我坏了），
 * 不说 404（没有这一格）——静态资源配错、目录没上传的那种事故，这里会对**每个**路径
 * 答 404，包括上海、东京那些明明有城的格。
 *
 * 它正常时做一件事：**把「有人问了某一格的目录分片，而那一格没有城」记成一行计数。**
 *
 * 为什么这是唯一可靠的信号：App 走到一座没有的城时会按格号来问一次分片（问完写下
 * `unmatched`，同一版目录下不再问）。这次请求就是「有人在那儿走路而我们没有图」的
 * 全部证据。想过发一份「哪些格有城」的索引让 App 本地判、省掉这次请求 —— 省掉的
 * 正是信号，所以留着它。
 *
 * 计数的口径是**散步条数，不是人数**：App 每条散步各问一次自己起点那一格，而 404 不进
 * 它的本地缓存（`LiveCityAtlas.fetch` 只缓存成功的响应）。所以一个人在同一格走三十趟
 * 就是三十行，开一次历史归属能一分钟内攒几十行。读排行榜要看**有哪些格上榜**，
 * 别读绝对值——单个重度用户顶得上一群人。
 *
 * **只记路径里那两个数。** 不记 IP、不记 UA、不记任何标识，也不看 cf 对象里的地理字段。
 * 周快照那条路（`usage` Worker）被有意隔开：那边带匿名 ID，把格号挂上去就等于按人
 * 攒一份粗粒度轨迹，与「不建自有轨迹库」直接冲突。这里反过来 —— 有格号、没有人。
 */

/** `directory/v<版本>/<纬度>_<经度>.json.gz`，格号是 1° 见方那一格的西南角整数度。 */
const SHARD_PATH = /^\/directory\/v(\d+)\/(-?\d+_-?\d+)\.json\.gz$/;

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    const shard = SHARD_PATH.exec(pathname);

    // 自证：主源此刻还在发这一版目录吗？答不上来就别替它说「没有」。
    // 只在没命中的路径上多问这一次，命中的请求根本不会进到这里。
    const latest = await env.ASSETS?.fetch(new URL("/directory/latest.json", request.url));
    if (latest && !latest.ok) {
      return new Response("Service Unavailable\n", {
        status: 503,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    // 地图包与别的东西没命中也是 404，但那不是信号：包是按 cityID 取的，
    // 取不到说明版本对不上或包被删了，与「有人在哪儿走路」无关。
    if (shard) {
      const [, version, cell] = shard;
      // 绑定没配也照常工作（账户还没开 Analytics Engine 时部署仍然能用）。
      // 统计掉了只是少一份排行，不该让用户的请求跟着失败。
      env.MISSES?.writeDataPoint({
        blobs: [`v${version}`, cell],
        doubles: [1],
        indexes: [cell],
      });
    }

    return new Response("Not Found\n", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
