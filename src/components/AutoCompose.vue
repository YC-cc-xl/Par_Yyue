<template>
  <div class="compose-page">
    <!-- ================= 顶部标题 ================= -->
    <div class="page-header">
      <span class="title">自动合成</span>
      <span class="subtitle">批量组合：视频 + 配音 + 字幕 + BGM</span>
    </div>

    <div class="page-body">
      <!-- ================= 0. 全局统计栏 + 分集明细 ================= -->
      <el-card shadow="never" class="panel">
        <div class="global-stats">
          <span class="stat-item">一级项目：<b>{{ scanSummary.projectTotal }}</b> 个</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">有效分集：<b>{{ scanSummary.validEpisodeTotal }}</b> 个</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">素材组：<b>{{ scanSummary.audioTotal }}</b> 组</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">待渲染：<b>{{ scanSummary.groupPendingTotal }}</b> 组</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">续跑跳过：<b>{{ scanSummary.groupResumedTotal }}</b> 组</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">视频素材：<b>{{ scanSummary.videoTotal }}</b> 个</span>
          <span class="stat-divider">|</span>
          <span class="stat-item">
            当前阶段：
            <el-tag :type="stageTagType" size="small" effect="plain">{{ stageLabel }}</el-tag>
          </span>
          <span class="stat-divider">|</span>
          <span class="stat-item">已运行：<b>{{ elapsedText }}</b></span>
        </div>

        <!-- 一行 = 一个分集文件夹 -->
        <el-table
          v-if="episodeDetail.length"
          :data="episodeDetail"
          size="small"
          border
          stripe
          class="mt8"
          max-height="280"
        >
          <el-table-column type="index" label="#" width="52" align="center" />
          <el-table-column prop="projectName" label="项目" width="120" show-overflow-tooltip />
          <el-table-column prop="name" label="分集目录" min-width="170" show-overflow-tooltip />
          <el-table-column prop="audioCount" label="素材组" width="80" align="center" />
          <el-table-column prop="videoCount" label="视频" width="76" align="center" />
          <el-table-column prop="resumedCount" label="续跑" width="70" align="center" />
          <el-table-column label="分集进度" min-width="170">
            <template #default="{ row }">
              <el-progress
                :percentage="row.progress || 0"
                :status="rowProgressStatus(row)"
                :stroke-width="10"
              />
            </template>
          </el-table-column>
          <el-table-column label="分集状态" width="100" align="center">
            <template #default="{ row }">
              <span :style="{ color: rowStatusColor(row.status) }">{{ rowStatusLabel(row.status) }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="尚未扫描，暂无分集明细" :image-size="60" />
      </el-card>

      <!-- ================= 全局总进度（表格下方、日志窗口上方） ================= -->
      <el-card shadow="never" class="panel">
        <div class="row-inline">
          <el-progress
            class="global-progress"
            :percentage="globalProgress"
            :status="globalProgressStatus"
            :stroke-width="14"
          />
          <span class="progress-text">
            {{ globalProgress }}% (已完成 {{ groupProgress.done }} / 总共 {{ groupProgress.total }})
          </span>
        </div>
      </el-card>

      <!-- ================= 1. 参数预设 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">参数预设</span></template>
        <div class="row-inline">
          <el-select
            v-model="presetId"
            placeholder="请选择预设"
            clearable
            style="width: 260px"
            :loading="presetLoading"
            @change="handlePresetChange"
          >
            <el-option v-for="p in presets" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <el-button type="primary" @click="savePresetDialog = true">保存预设</el-button>
          <el-button type="danger" plain :disabled="presetId == null" @click="handleDeletePreset">删除预设</el-button>
        </div>
      </el-card>

      <!-- ================= 2. 处理来源（批量一级项目列表） ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">处理来源</span></template>
        <div class="row-inline">
          <el-button type="primary" plain @click="handleBrowseSource">添加项目文件夹</el-button>
          <el-button type="success" :loading="scanning" :disabled="!form.sources.length" @click="handleScan">
            扫描目录
          </el-button>
          <span class="value-text">
            新增：一级文件夹 = 项目根目录，其下子文件夹 = 分集；分集内需同时存在【视频】【音频】两个文件夹
          </span>
        </div>

        <!-- 项目列表：每个项目一份「合并视频 / 加原片头」勾选（默认均取消勾选），并支持项目级暂停/取消 -->
        <div class="episode-list" v-if="form.sources.length">
          <div class="list-head">
            <span>一级项目列表</span>
            <el-tag type="info" effect="plain">共 {{ form.sources.length }} 个项目</el-tag>
          </div>
          <el-table :data="form.sources" size="small" border stripe max-height="220">
            <el-table-column type="index" label="#" width="52" align="center" />
            <el-table-column label="项目根目录" min-width="320" show-overflow-tooltip>
              <template #default="{ row }">
                <div>{{ row.root }}</div>
                <!-- 任务运行中：显示该项目进度与暂停/取消状态 -->
                <div class="file-cell" v-if="projectRuntime[row.index]">
                  进度 {{ projectRuntime[row.index].done }} / {{ projectRuntime[row.index].total }} 组
                  <el-tag v-if="projectRuntime[row.index].canceled" type="info" size="small" effect="plain">已取消</el-tag>
                  <el-tag v-else-if="projectRuntime[row.index].paused" type="warning" size="small" effect="plain">已暂停</el-tag>
                  <el-tag v-else-if="projectRuntime[row.index].merged" type="success" size="small" effect="plain">已合并</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="创建合并视频" width="120" align="center">
              <template #default="{ row }">
                <el-checkbox v-model="row.mergeVideo" />
              </template>
            </el-table-column>
            <el-table-column label="合并原视频与第一集" width="160" align="center">
              <template #default="{ row }">
                <el-checkbox v-model="row.mergeWithFirst" :disabled="!row.mergeVideo" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="250" align="center">
              <template #default="{ row }">
                <template v-if="isTaskRunning && projectRuntime[row.index]">
                  <el-button
                    link
                    type="warning"
                    size="small"
                    :disabled="projectRuntime[row.index].canceled"
                    @click="handleTogglePauseProject(row)"
                  >
                    {{ projectRuntime[row.index].paused ? '恢复' : '暂停' }}
                  </el-button>
                  <el-button
                    link
                    type="danger"
                    size="small"
                    :disabled="projectRuntime[row.index].canceled"
                    @click="handleCancelProject(row)"
                  >
                    取消本项目
                  </el-button>
                </template>
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="isTaskRunning"
                  @click="handleRemoveSource(row)"
                >
                  移除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="hint-text">
            合并按项目独立控制：某项目勾选就只合并该项目；合并成片输出到该项目的【一级项目根目录】下的「合并视频.mp4」。
            仅当该项目全部分集渲染结束后才会执行合并，且全局同一时间只运行 1 个合并任务。
          </div>
        </div>
        <el-empty v-else description="尚未添加项目，请点击「添加项目文件夹」" :image-size="70" />

        <!-- 扫描状态提示 -->
        <div class="scan-status" v-if="scanStatus">
          <el-alert :title="scanStatus" :type="scanStatusType" :closable="false" show-icon />
        </div>

        <!-- 项目级告警（如某个二级分集缺【视频】/【音频】文件夹，整集跳过） -->
        <div class="scan-status" v-for="(item, i) in projectWarnings" :key="i">
          <el-alert :title="`[项目${item.projectName}] ${item.text}`" type="warning" :closable="false" show-icon />
        </div>

        <!-- 分集清单 -->
        <div class="episode-list" v-if="episodes.length">
          <div class="list-head">
            <span>分集清单</span>
            <el-tag type="info" effect="plain">共 {{ episodes.length }} 集</el-tag>
          </div>
          <el-table :data="episodes" size="small" border stripe height="240">
            <el-table-column type="index" label="#" width="52" align="center" />
            <el-table-column prop="projectName" label="项目" width="120" show-overflow-tooltip />
            <el-table-column prop="name" label="分集目录" min-width="160" show-overflow-tooltip />
            <el-table-column label="视频" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.videoPath ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.videoPath ? '已匹配' : '缺失' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="配音" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.audioPath ? 'success' : 'danger'" size="small" effect="plain">
                  {{ row.audioPath ? '已匹配' : '缺失' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="字幕" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.subtitlePath ? 'success' : 'warning'" size="small" effect="plain">
                  {{ row.subtitlePath ? '已匹配' : '无' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="匹配文件" min-width="220">
              <template #default="{ row }">
                <div class="file-cell">{{ row.videoPath || '-' }}</div>
                <div class="file-cell">{{ row.audioPath || '-' }}</div>
                <div class="file-cell">{{ row.subtitlePath || '-' }}</div>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <el-empty v-else description="尚未扫描，请先选择根目录后点击「扫描目录」" :image-size="70" />
      </el-card>

      <!-- ================= 3. 背景音乐 BGM ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">背景音乐 BGM</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.bgmEnabled">启用背景音乐</el-checkbox>
          <span class="field-label">模式</span>
          <el-select v-model="form.bgmMode" style="width: 160px" :disabled="!form.bgmEnabled">
            <el-option label="单文件循环" value="single_loop" />
          </el-select>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">BGM来源</span>
          <el-input v-model="form.bgmFile" placeholder="请选择 BGM 文件" :disabled="!form.bgmEnabled" style="flex: 1" />
          <el-button type="primary" plain :disabled="!form.bgmEnabled" @click="handleBrowseBgm">浏览</el-button>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">音量</span>
          <el-slider
            v-model="form.bgmVolume"
            :min="0"
            :max="100"
            :disabled="!form.bgmEnabled"
            style="flex: 1; max-width: 420px"
          />
          <span class="value-text">{{ form.bgmVolume }}%</span>
        </div>
      </el-card>

      <!-- ================= 4. 字幕 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">字幕</span></template>
        <div class="row-inline">
          <el-checkbox v-model="form.subtitleEnabled">添加字幕</el-checkbox>
          <el-button type="primary" plain :disabled="!form.subtitleEnabled" @click="openSubtitleDialog">
            设置样式
          </el-button>
          <span class="value-text">
            {{ form.subtitleStyle.fontFamily }} / {{ form.subtitleStyle.fontSize }}px /
            {{ form.subtitleStyle.color }} / 描边{{ form.subtitleStyle.outlineWidth }}px /
            {{ positionLabel }}
          </span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">字体目录</span>
          <el-input
            v-model="form.fontsDir"
            placeholder="字体目录绝对路径（建议只放需要的字体，勿直接指向系统字体目录）"
            :disabled="!form.subtitleEnabled"
            style="flex: 1"
          />
        </div>
        <div class="row-inline mt8">
          <span class="field-label">字体文件</span>
          <el-input
            v-model="form.fontFile"
            placeholder="字体文件绝对路径（可选，如 D:\fonts\msyh.ttc）"
            :disabled="!form.subtitleEnabled"
            style="flex: 1"
          />
        </div>
        <div class="hint-text">不勾选「添加字幕」时不烧制字幕，仅做音视频合成。</div>
        <div class="hint-text">
          字幕渲染优先使用 ASS：源字幕为 .ass/.ssa 时直接使用，为 .srt 时自动预转为带样式的 ASS 再渲染。
        </div>
        <div class="hint-text">
          配置字体目录后，libass 直接定位字体、省去字体查找耗时，避免「字体缺失 → 回退别的字体」造成的渲染异常；建议指向只放所需字体的专用目录，不要直接指向系统字体目录。仅填「字体文件」时，后端会自动复制该字体到私有字体目录再渲染，只加载这一个字体。
        </div>
        <div class="hint-text">
          默认已配置 <b>C:\Windows\Fonts\msyh.ttc</b>（微软雅黑，Windows 自带）：渲染时只加载这一个字体，既消除字体查找耗时，也避免指向整个系统字体目录导致 libass 加载崩溃。
        </div>
      </el-card>

      <!-- ================= 5. 输出选项 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">输出选项</span></template>
        <div class="row-inline">
          <span class="field-label">输出位置</span>
          <span class="value-text">
            每个分集的成品 → 该分集内的【处理完成】文件夹；合并成片 → 一级项目根目录下的「合并视频.mp4」
          </span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">自定义分辨率</span>
          <el-input-number v-model="form.width" :min="128" :max="7680" :step="2" controls-position="right" style="width: 130px" />
          <span class="x-sign">×</span>
          <el-input-number v-model="form.height" :min="128" :max="7680" :step="2" controls-position="right" style="width: 130px" />
          <span class="unit-text">px</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">编码引擎</span>
          <el-select v-model="form.engine" style="width: 180px">
            <el-option label="自动（推荐）" value="auto" />
            <el-option label="CPU 软编码" value="cpu" />
            <el-option label="N卡 NVENC" value="nvenc" />
            <el-option label="Intel QSV" value="qsv" />
            <el-option label="A卡 AMF" value="amf" />
          </el-select>
          <span class="field-label ml16">自定义码率</span>
          <el-input-number v-model="form.bitrate" :min="200" :max="50000" :step="100" controls-position="right" style="width: 150px" />
          <span class="unit-text">kbps</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">编码预设</span>
          <el-select v-model="form.preset" style="width: 150px">
            <el-option label="slow（最慢/画质最好）" value="slow" />
            <el-option label="medium" value="medium" />
            <el-option label="fast（推荐）" value="fast" />
            <el-option label="veryfast（最快）" value="veryfast" />
          </el-select>
          <span class="field-label ml16">CRF / CQ</span>
          <el-input-number v-model="form.crf" :min="0" :max="51" controls-position="right" style="width: 130px" />
          <span class="unit-text">软编码=CRF，硬件编码=CQ；0 = 关闭</span>
        </div>
        <div class="hint-text">
          提示：编码预设越快渲染越快，画质轻微下降；恒定质量数值越小画质越好（常用 18~28）。硬件编码时该值作为 CQ，自动替代软编码 CRF。
        </div>
        <div class="hint-text">
          编码引擎「自动」会探测本机可用硬件编码器：按 N卡 NVENC → Intel QSV → A卡 AMF 优先级选择，均不可用时自动降级为 CPU libx264 软编码；手动指定但本机不可用也会自动降级。
        </div>
        <div class="hint-text">
          注意：硬烧字幕（subtitles 滤镜）无法硬件加速，字幕叠加始终由 CPU 处理；硬件加速仅加速「视频编码」这一步。
        </div>
      </el-card>

      <!-- ================= 6. 性能设置 ================= -->
      <el-card shadow="never" class="panel">
        <template #header><span class="panel-title">性能设置</span></template>
        <!-- 调度模式二选一：高性能多任务（多核+独显） / 低配单任务串行（6核16G） -->
        <div class="row-inline">
          <span class="field-label">调度模式</span>
          <el-select v-model="form.scheduleMode" style="width: 260px">
            <el-option label="高性能多任务模式（多核 CPU + 独显）" value="multi" />
            <el-option label="低配单任务串行模式（普通 6 核 / 16G 内存）" value="serial" />
          </el-select>
          <span class="field-label ml16">调度策略</span>
          <el-select
            v-model="form.schedulePolicy"
            style="width: 180px"
            :disabled="form.scheduleMode === 'serial'"
          >
            <el-option label="项目优先（优先出片）" value="project_first" />
            <el-option label="均分任务（齐头并进）" value="even" />
          </el-select>
        </div>
        <div class="hint-text">
          高性能多任务模式：字幕/音频预处理多任务并发；硬件编码（NVENC/QSV/AMF）任务串行排队，同一时刻只跑 1 个硬件编码进程，CPU 软编码任务不受闸门限制。适合一次性放入 2 套独立 9 小时项目并发处理。
        </div>
        <div class="hint-text">
          低配单任务串行模式：同一时刻只允许运行 1 个 ffmpeg 渲染任务，任务排队串行执行；调度固定「项目优先」，完整跑完当前项目再自动处理队列中的下一个项目。编码参数固定 libx264 / preset=veryfast / CRF=24（约 60 分钟输出 1 条 9 小时 1080p 全字幕成片），资源占用平稳，不易触发限流。
        </div>
        <div class="row-inline mt8">
          <el-checkbox v-model="form.parallelEnabled" :disabled="form.scheduleMode === 'serial'">启用并行处理</el-checkbox>
          <span class="field-label ml16">最大任务数（线程池并发）</span>
          <el-input-number
            v-model="form.maxWorkers"
            :min="1"
            :max="32"
            :disabled="form.scheduleMode === 'serial' || !form.parallelEnabled"
            controls-position="right"
            style="width: 130px"
          />
        </div>
        <div class="row-inline mt8">
          <span class="field-label">性能上限</span>
          <el-slider v-model="form.perfLimit" :min="0" :max="100" style="flex: 1; max-width: 420px" />
          <span class="value-text">{{ form.perfLimit }}%</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">任务超时</span>
          <el-input-number v-model="form.taskTimeoutSec" :min="0" :max="86400" :step="600" controls-position="right" style="width: 150px" />
          <span class="unit-text">秒（超时自动强杀 ffmpeg；0 = 使用默认 21600）</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">内存上限</span>
          <el-input-number v-model="form.memoryLimitMB" :min="0" :max="65536" :step="256" controls-position="right" style="width: 150px" />
          <span class="unit-text">MB / 每进程（0 = 不限制）</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">CPU 阈值</span>
          <el-input-number v-model="form.cpuThreshold" :min="0" :max="100" controls-position="right" style="width: 130px" />
          <span class="unit-text">%（持续高于则暂停提交新任务；0 = 不启用）</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">磁盘IO阈值</span>
          <el-input-number v-model="form.ioThresholdMBps" :min="0" :max="5000" :step="10" controls-position="right" style="width: 150px" />
          <span class="unit-text">MB/s（高于则暂停提交新任务；0 = 不启用）</span>
        </div>
        <div class="row-inline mt8">
          <span class="field-label">时长统计</span>
          <span class="value-text">FFmpeg 渲染总时长：<b>{{ durationStats.renderText }}</b></span>
          <span class="value-text ml16">排队等待总时长：<b>{{ durationStats.queueText }}</b></span>
        </div>
        <div class="hint-text">
          提示：并发过高会导致 CPU / 磁盘 IO 打满，反而拖慢整体渲染速度，建议取 CPU 核心数的一半左右。
        </div>
        <div class="hint-text">
          两层并发互不冲突：Python 线程池「最大任务数」控制同时并行运行的 FFmpeg 进程数；FFmpeg 内部 -threads 控制单个视频编码的内部多线程。
        </div>
        <div class="hint-text">
          建议源素材目录和输出目录放置在 SSD；机械硬盘并发渲染容易触发 IO 瓶颈，渲染速度会明显下降。
        </div>
        <div class="hint-text">
          时长口径：进度统计只使用「FFmpeg 渲染总时长」，「排队等待总时长」不计入渲染耗时，避免误判视频编码速度。
        </div>
      </el-card>

      <!-- ================= 7. 系统资源监控（实时轮询） ================= -->
      <el-card shadow="never" class="panel">
        <template #header>
          <span class="panel-title">系统资源监控</span>
          <el-tag class="ml8" :type="resourceTagType" size="small" effect="plain">{{ resourceStatusLabel }}</el-tag>
        </template>
        <div class="row-inline">
          <span class="field-label">CPU 占用</span>
          <el-progress
            :percentage="Math.min(100, Math.round(resourceStats.cpuPercent))"
            :stroke-width="12"
            :status="resourceStats.cpuHigh ? 'exception' : undefined"
            style="width: 220px"
          />
          <span class="value-text">{{ resourceStats.cpuPercent }}%</span>
          <span class="field-label ml16">磁盘读</span>
          <span class="value-text">{{ resourceStats.diskReadMBps }} MB/s</span>
          <span class="field-label ml16">磁盘写</span>
          <span class="value-text">{{ resourceStats.diskWriteMBps }} MB/s</span>
        </div>
        <div class="hint-text">
          资源饱和时（CPU 持续高于阈值 / 磁盘 IO 超过阈值）后台会临时停止提交新任务，等待资源回落再继续入队，避免系统卡死。
          <template v-if="!resourceStats.supported">（当前系统不支持资源监控，数据不可用）</template>
        </div>
      </el-card>

      <!-- ================= 任务进度 ================= -->
      <el-card shadow="never" class="panel" v-if="task">
        <template #header>
          <span class="panel-title">合成进度</span>
          <el-tag class="ml8" :type="taskTagType" effect="dark">{{ taskStatusLabel }}</el-tag>
        </template>
        <div class="row-inline">
          <span class="stat">总分集：<b>{{ task.total }}</b></span>
          <span class="stat success">成功组：<b>{{ task.success }}</b></span>
          <span class="stat danger">失败组：<b>{{ task.failed }}</b></span>
        </div>
        <el-progress class="mt8" :percentage="task.progress" :status="taskStatus" :stroke-width="14" />
        <div class="log-title">
          <span>运行日志</span>
          <el-select v-model="logLevel" size="small" style="width: 170px">
            <el-option label="全部（DEBUG）" value="DEBUG" />
            <el-option label="信息及以上（INFO）" value="INFO" />
            <el-option label="仅警告与错误（WARN）" value="WARN" />
            <el-option label="仅报错（ERROR）" value="ERROR" />
          </el-select>
        </div>
        <pre class="log-box">{{ filteredLog || '（暂无日志）' }}</pre>
      </el-card>
    </div>

    <!-- ================= 底部操作栏 ================= -->
    <div class="page-footer">
      <span class="footer-tip">{{ footerTip }}</span>
      <el-button
        v-if="isTaskRunning"
        type="warning"
        plain
        size="large"
        :loading="canceling"
        @click="handleCancelTask"
      >
        取消任务
      </el-button>
      <el-button type="primary" size="large" :loading="submitting" :disabled="scanning || isTaskRunning" @click="handleStartCompose">
        开始合成
      </el-button>
    </div>

    <!-- ================= 弹窗：保存预设 ================= -->
    <el-dialog v-model="savePresetDialog" title="保存参数预设" width="420px">
      <el-input v-model="presetNameInput" placeholder="请输入预设名称" maxlength="40" />
      <template #footer>
        <el-button @click="savePresetDialog = false">取消</el-button>
        <el-button type="primary" @click="handleSavePreset">保存</el-button>
      </template>
    </el-dialog>

    <!-- ================= 弹窗：字幕样式 ================= -->
    <el-dialog v-model="subtitleDialog" title="字幕样式设置" width="520px">
      <el-form label-width="86px">
        <el-form-item label="字体">
          <el-select v-model="styleDraft.fontFamily" style="width: 100%">
            <el-option v-for="f in fontOptions" :key="f" :label="f" :value="f" />
          </el-select>
        </el-form-item>
        <el-form-item label="字号">
          <el-input-number v-model="styleDraft.fontSize" :min="12" :max="120" controls-position="right" />
          <span class="unit-text ml8">px</span>
        </el-form-item>
        <el-form-item label="颜色">
          <el-color-picker v-model="styleDraft.color" />
          <span class="value-text ml8">{{ styleDraft.color }}</span>
        </el-form-item>
        <el-form-item label="描边颜色">
          <el-color-picker v-model="styleDraft.outlineColor" />
          <span class="value-text ml8">{{ styleDraft.outlineColor }}</span>
        </el-form-item>
        <el-form-item label="描边宽度">
          <el-input-number v-model="styleDraft.outlineWidth" :min="0" :max="10" controls-position="right" />
          <span class="unit-text ml8">px</span>
        </el-form-item>
        <el-form-item label="字幕位置">
          <el-select v-model="styleDraft.position" style="width: 100%">
            <el-option label="底部" value="bottom" />
            <el-option label="居中" value="center" />
            <el-option label="顶部" value="top" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="subtitleDialog = false">取消</el-button>
        <el-button type="primary" @click="handleConfirmSubtitleStyle">确定</el-button>
      </template>
    </el-dialog>

    <!-- ================= 弹窗：任务结束提示（全部成功 / 存在失败项） ================= -->
    <!-- 仅【确定】关闭按钮；中途手动取消任务不弹此窗 -->
    <el-dialog
      v-model="completionDialog"
      :title="completionInfo.title"
      width="420px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <div class="completion-line">总耗时：<b>{{ completionInfo.elapsed }}</b></div>
      <div class="completion-line">成功组数：<b>{{ completionInfo.success }}</b> 组</div>
      <div class="completion-line">失败组数：<b>{{ completionInfo.failed }}</b> 组</div>
      <template #footer>
        <el-button type="primary" @click="completionDialog = false">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

// ---------- 可选字体（Windows 常见字体） ----------
const fontOptions = ['微软雅黑', '黑体', '宋体', '楷体', '思源黑体', 'Arial']

// ---------- 页面表单：字段名与后端预设/任务结构保持一致 ----------
// 线程池最大并发任务数默认取 CPU 核心数的一半（并发过高会打满 CPU/磁盘 IO 反而拖慢速度）
const CPU_CORES = navigator.hardwareConcurrency || 4
const DEFAULT_MAX_WORKERS = Math.max(1, Math.floor(CPU_CORES / 2))

const form = reactive({
  // 处理来源：批量一级项目列表。每项 = { root, mergeVideo, mergeWithFirst }
  // mergeVideo/mergeWithFirst 为「按项目独立控制」的合并勾选，默认均取消勾选（false）
  sources: [],
  // BGM
  bgmEnabled: false,
  bgmMode: 'single_loop',
  bgmFile: '',
  bgmVolume: 20,
  // 字幕
  subtitleEnabled: false,
  subtitleStyle: {
    fontFamily: '微软雅黑',
    fontSize: 28,
    color: '#FFFFFF',
    outlineColor: '#000000',
    outlineWidth: 2,
    position: 'bottom'
  },
  // 字体绝对路径（实测默认值）：Windows 自带「微软雅黑」msyh.ttc，后端会复制到私有字体目录后
  // 只加载这一个字体，省掉字体查找耗时并避免字体缺失导致的渲染异常；字体目录留空即为默认策略
  fontFile: 'C:\\Windows\\Fonts\\msyh.ttc',
  fontsDir: '',
  // 输出选项（合并开关已移到「处理来源」的项目列表内，按项目独立控制，默认取消勾选）
  // 默认输出 1080p（典型场景：9 小时全程字幕，字幕渲染压力大）
  width: 1920,
  height: 1080,
  bitrate: 2000,
  preset: 'fast',   // h.264 编码预设
  crf: 0,           // 软编码=CRF / 硬件编码=CQ；>0 时以恒定质量编码
  engine: 'auto',   // 编码引擎：auto / cpu / nvenc / qsv / amf
  // 性能设置
  // 调度模式：multi 高性能多任务（原有，多核 + 独显） / serial 低配单任务串行（新增，6核16G）
  scheduleMode: 'multi',
  // 调度策略：project_first 项目优先 / even 均分任务（串行模式下固定项目优先，页面置灰）
  schedulePolicy: 'project_first',
  parallelEnabled: true,
  maxWorkers: DEFAULT_MAX_WORKERS,
  perfLimit: 80,
  taskTimeoutSec: 21600,   // 单集/拼接 ffmpeg 执行超时（秒，0=用默认）
  memoryLimitMB: 0,        // 单进程内存上限（MB，0=不限制）
  cpuThreshold: 90,        // CPU 持续高于该值 → 暂停提交新任务（0=不启用）
  ioThresholdMBps: 500     // 磁盘吞吐高于该值 → 暂停提交新任务（0=不启用）
                           // 实测本机 NVMe SSD 持续写入约 1074~1215 MB/s，取约 40% 作为更早介入的饱和保护阈值
})

// ---------- 参数预设 ----------
const presets = ref([])
const presetId = ref(null)
const presetLoading = ref(false)
const savePresetDialog = ref(false)
const presetNameInput = ref('')

// ---------- 扫描状态 ----------
const scanning = ref(false)
const scanStatus = ref('')
const scanStatusType = ref('info')
const episodes = ref([])
// 项目级告警（如某个二级分集缺【视频】/【音频】文件夹，整集跳过）
const projectWarnings = ref([])

// ---------- 全局统计栏 / 分集明细（扫描接口返回的 summary + detail） ----------
const scanSummary = ref({
  projectTotal: 0,          // 一级项目数
  episodeTotal: 0,
  validEpisodeTotal: 0,
  audioTotal: 0,            // 合格素材组总数（mp4+mp3+srt 同名）
  groupPendingTotal: 0,     // 待渲染组数
  groupResumedTotal: 0,     // 断点续跑：成品已存在而跳过的组数
  videoTotal: 0
})
const episodeDetail = ref([])

// ---------- 项目级运行态（progress 接口返回的 projects，驱动项目行的暂停/恢复/取消按钮） ----------
const projectStates = ref([])
// index → 项目运行态；未开始渲染时为空，按钮区不展示控制项
const projectRuntime = computed(() => {
  const map = {}
  for (const item of projectStates.value) map[item.index] = item
  return map
})
// 全局总进度（按「有效素材组」计）：已完成组数 / 总组数
const groupProgress = ref({ done: 0, total: 0 })
// 当前阶段：ready 准备就绪 / running 处理中 / completed 全部完成 / partial 部分失败
const composeStage = ref('ready')

// ---------- 已运行时长（全局计时，统计栏实时刷新） ----------
// 数值以后端任务状态接口返回的 elapsedSeconds 为准；任务结束（终态）后计时停止
const elapsedSeconds = ref(0)
const elapsedText = computed(() => formatDuration(elapsedSeconds.value))

// ---------- 双时长统计（性能限制卡片展示）----------
// renderText = FFmpeg 实际渲染总时长（进度统计只用它）；queueText = 排队等待总时长（不计入渲染耗时）
const durationStats = ref({ renderText: '00:00:00', queueText: '00:00:00' })

// ---------- 日志分级过滤 ----------
// 后端每行日志带 [DEBUG]/[INFO]/[WARN]/[ERROR] 前缀；旧行无前缀视为 INFO。
// 用户可选择「只看报错」屏蔽全部正常任务日志，保持日志窗口干净。
const logLevel = ref('INFO')
const LOG_ORDER = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 }
const filteredLog = computed(() => {
  const raw = task.value?.log || ''
  if (!raw) return ''
  const min = LOG_ORDER[logLevel.value] ?? LOG_ORDER.INFO
  return raw
    .split('\n')
    .filter((line) => {
      const matched = /^\[(DEBUG|INFO|WARN|ERROR)\]/.exec(line)
      const level = matched ? LOG_ORDER[matched[1]] : LOG_ORDER.INFO
      return level >= min
    })
    .join('\n')
})

// ---------- 系统资源监控（页面每 2 秒轮询后端快照）----------
const resourceStats = ref({
  supported: true,
  cpuPercent: 0,
  diskReadMBps: 0,
  diskWriteMBps: 0,
  processCount: 0,
  cpuHigh: false,
  ioHigh: false,
  throttled: false
})
let resourceTimer = null

// 资源状态标签：饱和（后台已暂停提交新任务）/ 正常 / 不支持
const resourceStatusLabel = computed(() => {
  if (!resourceStats.value.supported) return '不支持'
  return resourceStats.value.throttled ? '资源饱和 · 已暂停提交' : '正常'
})
const resourceTagType = computed(() => {
  if (!resourceStats.value.supported) return 'info'
  return resourceStats.value.throttled ? 'danger' : 'success'
})

async function refreshResources() {
  try {
    const res = await api.getSystemResources()
    if (res) resourceStats.value = { ...resourceStats.value, ...res }
  } catch {
    // 后端未就绪时静默忽略，等待下次轮询
  }
}

// ---------- 任务结束弹窗（全部成功 / 存在失败项，仅【确定】关闭） ----------
const completionDialog = ref(false)
const completionInfo = reactive({ title: '', elapsed: '00:00:00', success: 0, failed: 0 })
// 守卫：同一个任务只弹一次（轮询可能重复读到终态）
const completionNotifiedId = ref(null)

// 秒数 → 时:分:秒（如 00:02:15）
function formatDuration(totalSeconds) {
  const total = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const h = String(Math.floor(total / 3600)).padStart(2, '0')
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0')
  const s = String(total % 60).padStart(2, '0')
  return `${h}:${m}:${s}`
}

// ---------- 字幕样式弹窗 ----------
const subtitleDialog = ref(false)
// 草稿：确认后才写回 form.subtitleStyle，取消不影响原值
const styleDraft = reactive({ ...form.subtitleStyle })

// ---------- 任务与轮询 ----------
const submitting = ref(false)
const canceling = ref(false)
const task = ref(null)
let pollTimer = null

// 任务是否仍在进行（进行中才展示【取消任务】按钮）
const isTaskRunning = computed(() => ['pending', 'running'].includes(task.value?.status))

// ---------- 派生展示 ----------
const positionLabel = computed(() => {
  const map = { bottom: '底部', center: '居中', top: '顶部' }
  return map[form.subtitleStyle.position] || '底部'
})

const footerTip = computed(() => {
  if (!form.sources.length) return '请先添加一级项目根目录'
  if (!episodes.value.length) return '请先扫描目录，确认分集清单'
  const mergeCount = form.sources.filter((s) => s.mergeVideo).length
  const mergeTip = mergeCount ? `（${mergeCount} 个项目将执行合并）` : '（未勾选合并）'
  return `共 ${form.sources.length} 个项目 / ${episodes.value.length} 集待合成 ${mergeTip}`
})

// 当前阶段文案 / 标签色（统计栏第 4 项）
const stageLabel = computed(() => {
  const map = {
    ready: '准备就绪',
    running: '处理中',
    completed: '全部完成',
    partial: '部分失败',
    canceled: '已取消'
  }
  return map[composeStage.value] || '准备就绪'
})

const stageTagType = computed(() => {
  const map = { ready: 'info', running: 'warning', completed: 'success', partial: 'danger', canceled: 'info' }
  return map[composeStage.value] || 'info'
})

const taskStatusLabel = computed(() => {
  const map = { pending: '等待中', running: '合成中', completed: '已完成', failed: '失败', canceled: '已取消' }
  return map[task.value?.status] || '未知'
})

const taskTagType = computed(() => {
  const map = { pending: 'info', running: 'warning', completed: 'success', failed: 'danger', canceled: 'info' }
  return map[task.value?.status] || 'info'
})

// 进度条状态：完成显示成功图标，失败显示错误图标，其余普通
const taskStatus = computed(() => {
  if (task.value?.status === 'completed') return 'success'
  if (task.value?.status === 'failed') return 'exception'
  return undefined
})

// ---------- 全局总进度条 ----------
// 进度数值 = 已完成渲染的素材组数 ÷ 全部有效素材组数
const globalProgress = computed(() => {
  const { done, total } = groupProgress.value
  if (total > 0) return Math.min(100, Math.round((done / total) * 100))
  return Math.round(Number(task.value?.progress) || 0)
})

const globalProgressStatus = computed(() => {
  if (!task.value) return undefined
  if (composeStage.value === 'completed') return 'success'
  if (composeStage.value === 'partial') return 'exception'
  return undefined
})

// ---------- 分集行：进度 / 状态 ----------
const EPISODE_STATUS_TEXT = { pending: '待处理', running: '渲染中', success: '成功', failed: '失败', canceled: '已取消' }
const EPISODE_STATUS_COLOR = {
  pending: '#909399',
  running: '#409eff',
  success: '#67c23a',
  failed: '#f56c6c',
  canceled: '#e6a23c'
}

function rowStatusLabel(status) {
  return EPISODE_STATUS_TEXT[status] || EPISODE_STATUS_TEXT.pending
}

function rowStatusColor(status) {
  return EPISODE_STATUS_COLOR[status] || EPISODE_STATUS_COLOR.pending
}

function rowProgressStatus(row) {
  if (row.status === 'success') return 'success'
  if (row.status === 'failed') return 'exception'
  if (row.status === 'canceled') return 'warning'
  return undefined
}

// 把轮询到的分集运行态合并进明细行（保留扫描得到的静态字段）
// 注意：多项目并存时各项目分集序号会重复，必须用「项目-分集」复合键 key 匹配
function applyEpisodeProgress(rows) {
  if (!rows || !rows.length) return
  const runtimeMap = new Map(rows.map((item) => [item.key, item]))
  episodeDetail.value = episodeDetail.value.map((row) => {
    const runtime = runtimeMap.get(row.key)
    if (!runtime) return row
    return {
      ...row,
      progress: runtime.total ? (runtime.done / runtime.total) * 100 : 0,
      status: runtime.status,
      done: runtime.done,
      total: runtime.total
    }
  })
}

// ---------- 1. 参数预设 ----------
// 加载预设列表（阶段8 联调）
async function loadPresets() {
  presetLoading.value = true
  try {
    presets.value = (await api.listPresets()) || []
  } catch {
    // 后端未就绪时静默处理，不阻塞界面
    presets.value = []
  } finally {
    presetLoading.value = false
  }
}

// 选择预设后，将预设内容回填到表单
function handlePresetChange(id) {
  if (id == null) return
  const preset = presets.value.find((p) => p.id === id)
  if (!preset) return
  try {
    const cfg = typeof preset.config === 'string' ? JSON.parse(preset.config) : preset.config
    Object.keys(form).forEach((key) => {
      if (cfg[key] !== undefined) form[key] = cfg[key]
    })
    if (cfg.subtitleStyle) Object.assign(form.subtitleStyle, cfg.subtitleStyle)
    // 预设可能带项目列表：重建序号并清空旧扫描结果（项目列表变化后扫描数据必然失效）
    if (Array.isArray(cfg.sources)) {
      reindexSources()
      resetScanResult()
    }
    ElMessage.success(`已应用预设：${preset.name}`)
  } catch {
    ElMessage.error('预设内容解析失败')
  }
}

// 保存当前整页表单配置为新预设
async function handleSavePreset() {
  const name = presetNameInput.value.trim()
  if (!name) {
    ElMessage.warning('请输入预设名称')
    return
  }
  try {
    await api.savePreset(name, JSON.parse(JSON.stringify(form)))
    ElMessage.success('预设已保存')
    savePresetDialog.value = false
    presetNameInput.value = ''
    await loadPresets()
  } catch (e) {
    ElMessage.error(`保存失败：${e.message}`)
  }
}

// 删除当前选中预设
async function handleDeletePreset() {
  const preset = presets.value.find((p) => p.id === presetId.value)
  if (!preset) return
  try {
    await ElMessageBox.confirm(`确定删除预设「${preset.name}」？`, '提示', { type: 'warning' })
  } catch {
    return // 用户取消
  }
  try {
    await api.deletePreset(preset.id)
    ElMessage.success('已删除')
    presetId.value = null
    await loadPresets()
  } catch (e) {
    ElMessage.error(`删除失败：${e.message}`)
  }
}

// ---------- 2. 处理来源 ----------
// 后端就绪检查：Electron 环境下可读到主进程状态，未就绪时给出可操作提示
async function ensureBackendReady() {
  try {
    const status = await window.electronAPI?.getBackendStatus?.()
    if (status && !status.ready) {
      const tail = (status.tail || []).slice(-1)[0]
      ElMessage.error(`后端服务未就绪：${status.error || tail || '未知原因'}`)
      return false
    }
  } catch {
    // 非 Electron 环境（浏览器调试）跳过检查
  }
  return true
}

// 清空扫描结果与任务展示态（项目列表变更后调用，避免残留旧数据）
function resetScanResult() {
  episodes.value = []
  scanStatus.value = ''
  projectWarnings.value = []
  scanSummary.value = {
    projectTotal: 0,
    episodeTotal: 0,
    validEpisodeTotal: 0,
    audioTotal: 0,
    groupPendingTotal: 0,
    groupResumedTotal: 0,
    videoTotal: 0
  }
  episodeDetail.value = []
  projectStates.value = []
  groupProgress.value = { done: 0, total: 0 }
  composeStage.value = 'ready'
  // 目录变更：计时归零，允许新任务结束弹窗
  elapsedSeconds.value = 0
  durationStats.value = { renderText: '00:00:00', queueText: '00:00:00' }
  completionDialog.value = false
  completionNotifiedId.value = null
}

// 重新编号项目序号：后端按「列表顺序」定项目序号，移除项目后必须同步，
// 保证页面 row.index 与后端 projects[].index 一一对应（项目级暂停/取消按钮依赖它）
function reindexSources() {
  form.sources.forEach((item, i) => {
    item.index = i + 1
  })
}

// 添加一级项目根目录（列表式逐个追加，已存在的项目不重复添加）
async function handleBrowseSource() {
  const selected = await window.electronAPI?.selectFolder?.()
  if (!selected) return
  if (form.sources.some((item) => item.root === selected)) {
    ElMessage.warning('该项目已在列表中')
    return
  }
  form.sources.push({ index: form.sources.length + 1, root: selected, mergeVideo: false, mergeWithFirst: false })
  reindexSources()
  resetScanResult()
}

// 从列表移除一个项目（任务运行中禁止移除，避免与后端项目序号错位）
function handleRemoveSource(row) {
  const position = form.sources.indexOf(row)
  if (position < 0) return
  form.sources.splice(position, 1)
  reindexSources()
  resetScanResult()
}

// 扫描全部项目目录（批量）：返回各项目/分集/素材组与断点续跑跳过信息
async function handleScan() {
  if (!form.sources.length) {
    ElMessage.warning('请先添加一级项目根目录')
    return
  }
  if (!(await ensureBackendReady())) return
  scanning.value = true
  scanStatus.value = '正在扫描目录…'
  scanStatusType.value = 'info'
  projectWarnings.value = []
  try {
    const res = await api.scanFolders(form.sources.map((item) => item.root))
    const projects = res?.projects || []
    // 分集清单：从各项目内取分集（带第一组有效素材路径，供「匹配文件」列展示）
    episodes.value = projects.flatMap((project) =>
      (project.episodes || []).map((episode) => ({
        ...episode,
        key: `${project.index}-${episode.index}`,
        projectIndex: project.index,
        projectName: project.name
      }))
    )
    // 统计栏 + 分集明细（detail 一行为一个二级分集）
    episodeDetail.value = (res?.detail || []).map((item) => ({
      ...item,
      progress: 0,
      status: 'pending'
    }))
    projectWarnings.value = res?.projectWarnings || []
    const summary = res?.summary || {}
    scanSummary.value = {
      projectTotal: summary.projectTotal ?? projects.length,
      episodeTotal: summary.episodeTotal ?? episodes.value.length,
      validEpisodeTotal:
        summary.validEpisodeTotal ?? episodes.value.filter((ep) => (ep.groups || []).length).length,
      // audioTotal 为「合格素材组总数」的兼容字段名
      audioTotal: summary.audioTotal ?? summary.groupTotal ?? 0,
      groupPendingTotal: summary.groupPendingTotal ?? 0,
      groupResumedTotal: summary.groupResumedTotal ?? 0,
      videoTotal: summary.videoTotal ?? 0
    }
    // 新一轮扫描：全局进度归零，总组数 = 全部有效素材组数
    groupProgress.value = { done: 0, total: scanSummary.value.audioTotal }
    // 扫描完成且尚未开始渲染 → 准备就绪；计时归零，允许下次任务结束弹窗
    composeStage.value = 'ready'
    projectStates.value = []
    elapsedSeconds.value = 0
    durationStats.value = { renderText: '00:00:00', queueText: '00:00:00' }
    completionDialog.value = false
    completionNotifiedId.value = null
    scanStatus.value =
      `扫描完成：共 ${scanSummary.value.projectTotal} 个项目 / ${scanSummary.value.validEpisodeTotal} 个有效分集 / ` +
      `${scanSummary.value.audioTotal} 组合格素材（待渲染 ${scanSummary.value.groupPendingTotal} 组，` +
      `续跑跳过 ${scanSummary.value.groupResumedTotal} 组）`
    scanStatusType.value = 'success'
  } catch (e) {
    resetScanResult()
    scanStatus.value = `扫描失败：${e.message}（需先启动后端服务）`
    scanStatusType.value = 'error'
  } finally {
    scanning.value = false
  }
}

// ---------- 2.1 项目级控制：单独暂停/恢复/取消某一个一级项目 ----------
// 暂停：该项目未开始的素材组暂不提交，已开始的渲染跑完；恢复后继续跑剩余组
async function handleTogglePauseProject(row) {
  if (!task.value?.id) return
  const runtime = projectRuntime.value[row.index]
  if (!runtime) return
  try {
    if (runtime.paused) {
      await api.resumeProject(task.value.id, row.index)
      ElMessage.success(`项目${runtime.name} 已恢复`)
    } else {
      await api.pauseProject(task.value.id, row.index)
      ElMessage.success(`项目${runtime.name} 已暂停`)
    }
    await refreshTask()
  } catch (e) {
    ElMessage.error(`项目控制失败：${e.message}`)
  }
}

// 取消：终止该项目所有未完成任务（在跑的 ffmpeg 立即强杀），不影响其他项目
async function handleCancelProject(row) {
  if (!task.value?.id) return
  const runtime = projectRuntime.value[row.index]
  if (!runtime) return
  try {
    await ElMessageBox.confirm(
      `确定取消项目「${runtime.name}」？该项目未完成的分集任务将被终止（其他项目不受影响）`,
      '提示',
      { type: 'warning' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await api.cancelProject(task.value.id, row.index)
    ElMessage.success(`项目${runtime.name} 已取消`)
    await refreshTask()
  } catch (e) {
    ElMessage.error(`取消项目失败：${e.message}`)
  }
}

// ---------- 3. BGM ----------
async function handleBrowseBgm() {
  const selected = await window.electronAPI?.selectBgmFile?.()
  if (selected) form.bgmFile = selected
}

// ---------- 4. 字幕样式 ----------
function openSubtitleDialog() {
  Object.assign(styleDraft, form.subtitleStyle)
  subtitleDialog.value = true
}

function handleConfirmSubtitleStyle() {
  Object.assign(form.subtitleStyle, styleDraft)
  subtitleDialog.value = false
}

// ---------- 5. 开始合成 ----------
function buildPayload() {
  const roots = form.sources.map((item) => item.root)
  return {
    // 项目列表：每项自带合并勾选 → 后端按项目独立控制合并（A 勾选只合并 A）
    projects: form.sources.map((item) => ({
      root: item.root,
      mergeVideo: !!item.mergeVideo,
      // 未勾选「创建合并视频」时不加片头
      mergeWithFirst: !!(item.mergeVideo && item.mergeWithFirst)
    })),
    sourceDirs: roots,
    bgm: {
      enabled: form.bgmEnabled,
      mode: form.bgmMode,
      file: form.bgmFile,
      volume: form.bgmVolume
    },
    subtitle: {
      enabled: form.subtitleEnabled,
      style: { ...form.subtitleStyle },
      fontFile: form.fontFile,
      fontsDir: form.fontsDir
    },
    output: {
      width: form.width,
      height: form.height,
      bitrate: form.bitrate,
      preset: form.preset,
      crf: form.crf,
      engine: form.engine
    },
    performance: {
      // 双调度模式：multi 高性能多任务 / serial 低配单任务串行
      scheduleMode: form.scheduleMode,
      // 串行模式下后端固定为项目优先（此处同步，避免前后端语义不一致）
      schedulePolicy: form.scheduleMode === 'serial' ? 'project_first' : form.schedulePolicy,
      parallelEnabled: form.parallelEnabled,
      maxWorkers: form.maxWorkers,
      perfLimit: form.perfLimit,
      taskTimeoutSec: form.taskTimeoutSec,
      memoryLimitMB: form.memoryLimitMB,
      cpuThreshold: form.cpuThreshold,
      ioThresholdMBps: form.ioThresholdMBps
    }
  }
}

async function handleStartCompose() {
  if (!form.sources.length) {
    ElMessage.warning('请先添加一级项目根目录')
    return
  }
  if (!episodes.value.length) {
    ElMessage.warning('请先扫描目录，确认分集清单')
    return
  }
  if (form.bgmEnabled && !form.bgmFile) {
    ElMessage.warning('已启用背景音乐，请选择 BGM 文件')
    return
  }
  if (!(await ensureBackendReady())) return
  submitting.value = true
  try {
    const res = await api.createTask(buildPayload())
    task.value = {
      id: res?.id,
      total: res?.total ?? 0,
      success: 0,
      failed: 0,
      progress: 0,
      status: 'running',
      log: ''
    }
    ElMessage.success('合成任务已创建')
    // 已开始渲染 → 处理中；进度归零重新计（总组数沿用扫描到的有效素材组数）
    composeStage.value = 'running'
    groupProgress.value = { done: 0, total: groupProgress.value.total }
    projectStates.value = []
    // 计时起点 = 后端记录的任务起始时间戳；新任务允许再次弹窗
    elapsedSeconds.value = res?.elapsedSeconds ?? 0
    durationStats.value = { renderText: '00:00:00', queueText: '00:00:00' }
    completionNotifiedId.value = null
    startPolling()
  } catch (e) {
    ElMessage.error(`创建任务失败：${e.message}（需先启动后端服务）`)
  } finally {
    submitting.value = false
  }
}

// 手动取消任务：计时停止、终态 canceled，不弹完成弹窗
async function handleCancelTask() {
  if (!task.value?.id) return
  canceling.value = true
  try {
    await api.cancelTask(task.value.id)
    ElMessage.success('已取消任务')
    stopPolling()
    composeStage.value = 'canceled'
    // 守卫：本任务不再触发任务结束弹窗
    completionNotifiedId.value = task.value.id
    // 拉取一次最终状态（计时在取消时刻停止）
    await refreshTask()
  } catch (e) {
    ElMessage.error(`取消失败：${e.message}`)
  } finally {
    canceling.value = false
  }
}

// 轮询任务进度（阶段8 联调）：800ms 间隔，降低高频请求开销且不影响进度观感
function startPolling() {
  stopPolling()
  pollTimer = setInterval(refreshTask, 800)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function refreshTask() {
  if (!task.value?.id) return
  try {
    // 进度接口同时返回任务状态/日志、各分集运行态与全局已运行时长，一次请求即可刷新全部区域
    const res = await api.getTaskProgress(task.value.id)
    if (!res) return
    task.value = { ...task.value, ...res }
    // 全局总进度：已完成渲染的素材组数 / 全部有效素材组数
    groupProgress.value = { done: res.doneGroups ?? 0, total: res.totalGroups ?? 0 }
    // 项目级运行态：驱动项目列表行的进度、暂停/恢复与取消按钮
    projectStates.value = res.projects || []
    // 已运行时长：以后端起始时间戳推导的秒数为准，随轮询实时刷新
    elapsedSeconds.value = res.elapsedSeconds ?? elapsedSeconds.value
    // 双时长：FFmpeg 实际渲染总时长（进度统计口径）/ 排队等待总时长
    durationStats.value = {
      renderText: res.renderText || '00:00:00',
      queueText: res.queueText || '00:00:00'
    }
    applyEpisodeProgress(res.episodes)
    if (res.status === 'canceled') {
      // 手动取消：计时在取消时刻停止，进度不再更新，且不弹完成弹窗
      stopPolling()
      elapsedSeconds.value = res.elapsedSeconds ?? elapsedSeconds.value
      composeStage.value = 'canceled'
      completionNotifiedId.value = task.value.id
      return
    }
    if (res.status === 'completed' || res.status === 'failed') {
      stopPolling()
      // 全部成功 → 全部完成；只要有素材组失败 → 部分失败
      composeStage.value = res.status === 'completed' && !res.failed ? 'completed' : 'partial'
      // 终态：计时停止（保留最终 elapsedSeconds），弹一次结束弹窗
      showCompletion(res)
    }
  } catch {
    // 轮询失败保持静默，等待下次重试
  }
}

// 任务执行完毕弹窗：全部成功 → 【任务全部完成】；存在失败 → 【任务处理完毕（存在失败项）】
// 同一任务只弹一次；中途手动取消任务不会走到终态，因此不弹此窗
function showCompletion(res) {
  const id = task.value?.id
  if (id == null || completionNotifiedId.value === id) return
  completionNotifiedId.value = id
  const success = res.success ?? task.value?.success ?? 0
  const failed = res.failed ?? task.value?.failed ?? 0
  completionInfo.title = failed > 0 ? '任务处理完毕（存在失败项）' : '任务全部完成'
  completionInfo.elapsed = res.elapsed || formatDuration(elapsedSeconds.value)
  completionInfo.success = success
  completionInfo.failed = failed
  completionDialog.value = true
}

// 组件卸载时清理定时器（任务轮询 + 资源监控轮询）
onBeforeUnmount(() => {
  stopPolling()
  if (resourceTimer) {
    clearInterval(resourceTimer)
    resourceTimer = null
  }
})

// 初始化：加载预设列表 + 启动系统资源监控轮询（2 秒一次，独立于任务轮询）
loadPresets()
refreshResources()
resourceTimer = setInterval(refreshResources, 2000)
</script>

<style scoped>
.compose-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

/* 顶部标题 */
.page-header {
  flex: none;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
}
.page-header .title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}
.page-header .subtitle {
  font-size: 12px;
  color: #909399;
}

/* 内容区可滚动 */
.page-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 8px;
}

.panel {
  margin-bottom: 14px;
  border-radius: 6px;
}
.panel :deep(.el-card__header) {
  padding: 10px 16px;
  background: #fafafa;
}
.panel :deep(.el-card__body) {
  padding: 14px 16px;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

/* 通用横排布局 */
.row-inline {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
/* 全局总进度：进度条占满，右侧同步显示组数文字 */
.global-progress {
  flex: 1;
  min-width: 240px;
}
.progress-text {
  color: #606266;
  font-size: 13px;
  white-space: nowrap;
}
.mt8 {
  margin-top: 8px;
}
.ml8 {
  margin-left: 8px;
}
.ml16 {
  margin-left: 16px;
}
.field-label {
  color: #606266;
  font-size: 13px;
}
.value-text {
  color: #909399;
  font-size: 13px;
}

/* 任务结束弹窗内容行 */
.completion-line {
  line-height: 30px;
  font-size: 14px;
  color: #606266;
}
.unit-text {
  color: #909399;
  font-size: 13px;
}
.x-sign {
  color: #909399;
  padding: 0 2px;
}
.hint-text {
  margin-top: 10px;
  font-size: 12px;
  color: #a8abb2;
  line-height: 1.6;
}

/* 扫描状态 */
.scan-status {
  margin-top: 10px;
}

/* 分集清单 */
.episode-list {
  margin-top: 12px;
}
.list-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.file-cell {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  word-break: break-all;
}

/* 全局统计栏（4 项文字信息并排） */
.global-stats {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.global-stats .stat-item {
  font-size: 14px;
  color: #606266;
}
.global-stats .stat-item b {
  color: #303133;
  font-weight: 600;
  padding: 0 2px;
}
.global-stats .stat-divider {
  color: #dcdfe6;
  margin: 0 12px;
}

/* 统计文字 */
.stat {
  font-size: 13px;
  color: #606266;
  margin-right: 18px;
}
.stat.success b {
  color: #67c23a;
}
.stat.danger b {
  color: #f56c6c;
}

/* 日志区：标题与「日志级别」下拉左右分布 */
.log-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.log-box {
  margin: 8px 0 0;
  height: 180px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  font-size: 12px;
  line-height: 1.6;
  padding: 10px 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: Consolas, "Courier New", monospace;
}

/* 底部操作栏 */
.page-footer {
  flex: none;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}
.footer-tip {
  font-size: 13px;
  color: #909399;
}
</style>
