# 上海高校就业网公开接口验证记录

日期：2026-09-07。范围：仅匿名公开页面和 JSON 请求的隔离验证；没有登录、验证码、Cookie、正式岗位库写入或自动发布。

## 结论

| 来源 | 公开入口 | 技术结果 | 当前状态 |
|---|---|---|---|
| 华东理工大学就业网 | `https://career.ecust.edu.cn/PositionList2.aspx/` | 首页、职位页和 `robots.txt` 均返回 HTTP 483，页面明确提示仅限校内访问 | 保持 B 类禁用，不尝试绕过限制 |
| 上海交通大学就业网实习 | `https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx` | 匿名表单 POST 列表与详情 JSON 可读；本次隔离读取 1 页、1 条详情，成功解析 10 个公告列表项和 1 个职位详情 | 已具备专用适配器，保持 B 类禁用 |
| 上海财经大学就业网招聘 | `https://career.sufe.edu.cn/career/zpxx/zpxx` | 匿名表单 POST 列表与详情 JSON 可读；本次隔离读取 1 页、1 条详情，成功解析 10 个公告列表项和 4 个职位详情 | 已具备专用适配器，保持 B 类禁用 |

## 已核验请求路径

- 交大实习：列表首屏 `POST /career/zpxx/search/sxzpxx`；后续页 `POST /career/zpxx/search/sxzpxx/{page_num}/{page_size}`；详情 `POST /career/zpxx/data/zpxx/{announcement_id}`。
- 上财招聘：列表首屏 `POST /career/zpxx/search/zpxx`；后续页 `POST /career/zpxx/search/zpxx/{page_num}/{page_size}`；详情 `POST /career/zpxx/data/zpxx/{announcement_id}`。
- 请求使用空表单体、12 秒单请求超时及项目既有公开信息 User-Agent；适配器不会发现接口、模拟浏览器、登录或绕过验证码。

## 数据质量边界

- 职位身份使用公告 ID 与职位 ID（`zpxxid:zwid`）；发布日期变化不会创建新岗位。
- 仅职位地点字段 `gzdz` 明确含“上海”，或无职位明细时正文存在明确的“工作地点/工作地址/岗位地点”段落，才进入上海待核验队列。
- `szxmc` 是行政区，不能充当招聘单位；单位仅使用 `dwmc`、`companyName` 或 `employerName`。
- 列表没有可点击的单公告详情链接时，记录学校已验证公开列表页作为人工回查入口；不猜测不存在的详情 URL。
- 来源仍为 B 类禁用。只有完成连续 3 次隔离试采成功、正文清洗合格并人工确认后，才可评估升 A；任何情况下都不自动发布。

## 验证结果

- 离线适配器与相邻来源测试：17 项通过。
- 全量测试：272 项通过，1 个既有第三方 `TestClient` 弃用警告。
- 后续：对交大、上财各完成 3 次隔离试采并保存逐轮报告；华东理工如访问策略改变，再重新评估公开接入。
