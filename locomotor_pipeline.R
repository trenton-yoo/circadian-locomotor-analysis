# =====================================================
# Locomotor activity pipeline (mice)
# Expected Excel layout (see sample2.xlsx):
#   Row 1: usually blank
#   Row 2: animal # (integer) in each animal column
#   Row 3: cage code (cell B3 may say "cage")
#   Row 4: animal label / genotype
#   Row 5+: col A optional date, col B time, col C+ counts
# Blank spacer columns and columns without an animal #
# are ignored. Reading stops at the first blank / non-time
# cell in column B (so trailing summary blocks are excluded).
# =====================================================

# Install once (uncomment if needed):
# install.packages(c(
#   "readxl", "dplyr", "tidyr", "lubridate", "stringr",
#   "purrr", "writexl", "tibble"
# ))

library(readxl)
library(dplyr)
library(tidyr)
library(lubridate)
library(stringr)
library(purrr)
library(tibble)
library(writexl)

# -------- CONFIG --------
# Path to Excel file. If NULL, uses the first *.xlsx in this folder
# (excluding wake_episodes.xlsx / sample_file.xlsx / temp files).
input_xlsx   <- "sample2.xlsx"
sheet        <- 1                 # sheet name or index
tz_out       <- "UTC"
output_xlsx  <- "wake_episodes.xlsx"

# Bin size in minutes. NULL = auto-detect from consecutive timestamps.
# Set manually (e.g. 5 or 10) only if you need to override detection.
bin_minutes  <- NULL

# Defaults used when not prompting (non-interactive) or if user presses Enter.
start_date_fallback <- as.Date("2020-01-01")
thresholds <- 100                 # activity count threshold (wake if counts >= this)
min_episode_minutes <- 10         # drop wake episodes shorter than this

# If TRUE and running interactively, after preprocessing ask for:
# threshold, min episode length, start date, and optional animal_label edits.
ask_user_parameters <- TRUE
# ------------------------

thresholds <- as.numeric(thresholds)
if (any(is.na(thresholds)) || length(thresholds) == 0) {
  stop("`thresholds` must be one or more numbers, e.g. 100 or c(50, 100, 150).")
}
min_episode_minutes <- as.numeric(min_episode_minutes)
if (is.na(min_episode_minutes) || min_episode_minutes < 0) {
  stop("`min_episode_minutes` must be a non-negative number.")
}

# =====================================================
# Helpers
# =====================================================

cell_chr <- function(x) {
  if (length(x) == 0 || is.null(x) || (length(x) == 1 && is.na(x))) {
    return(NA_character_)
  }
  x <- x[[1]]
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(NA_character_)
  str_squish(as.character(x))
}

is_blank_chr <- function(x) {
  is.na(x) || !nzchar(x) || tolower(x) %in% c("na", "null", "none")
}

is_summary_label <- function(s) {
  !is.na(s) && tolower(s) %in% c("dark", "light", "ratio", "cage")
}

# Excel stores times as day-fractions (0.795... = 19:05).
# After midnight, values often become 1.003..., 1.006..., etc.
# readxl frequently returns these as character strings. IMPORTANT:
# do NOT parse "1.25" as clock H:M — that was the prior bug (1:25).
excel_serial_to_sec <- function(num) {
  if (is.na(num) || num < 0) return(NA_real_)
  # Round to nearest second to avoid float artifacts like 19:09:59
  round((num %% 1) * 86400) %% 86400
}

# Convert Excel time / datetime / character values → seconds since midnight
to_sec_of_day <- function(x) {
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(NA_real_)
  x <- x[[1]]

  if (inherits(x, "Period")) {
    return(as.numeric(x, "seconds") %% 86400)
  }
  if (inherits(x, "hms") || inherits(x, "difftime")) {
    return(as.numeric(x) %% 86400)
  }
  if (inherits(x, "POSIXt") || inherits(x, "Date")) {
    x <- as.POSIXct(x, tz = tz_out)
    return(hour(x) * 3600 + minute(x) * 60 + second(x))
  }
  if (is.numeric(x) && !is.na(x)) {
    return(excel_serial_to_sec(x))
  }

  s <- cell_chr(x)
  if (is_blank_chr(s) || is_summary_label(s)) return(NA_real_)

  # Prefer Excel day-serial (character like "0.7951388888888888" or "1.25")
  num <- suppressWarnings(as.numeric(s))
  if (!is.na(num) && num >= 0) {
    return(excel_serial_to_sec(num))
  }

  # Fallback: human-readable clock strings
  parsed <- parse_date_time(
    toupper(str_replace_all(s, "\u00A0", " ")),
    orders = c("I:M p", "I:M%p", "I:M:%S p", "I:M:S p",
               "H:M:S", "H:M", "Ymd HMS", "Ymd HM"),
    tz = tz_out,
    exact = FALSE,
    quiet = TRUE
  )
  if (is.na(parsed)) return(NA_real_)
  hour(parsed) * 3600 + minute(parsed) * 60 + second(parsed)
}

extract_date <- function(x) {
  if (is.null(x) || (length(x) == 1 && is.na(x))) return(as.Date(NA))
  x <- x[[1]]
  if (inherits(x, "Date")) return(x)
  if (inherits(x, "POSIXt")) return(as.Date(x, tz = tz_out))
  if (is.numeric(x) && !is.na(x)) {
    # Unix seconds (readxl sometimes returns these)
    if (x > 1e9 && x < 2e10) {
      return(as.Date(as.POSIXct(x, origin = "1970-01-01", tz = tz_out), tz = tz_out))
    }
    # Excel date serial
    if (x > 20000 && x < 100000) {
      return(as.Date(x, origin = "1899-12-30"))
    }
  }
  s <- cell_chr(x)
  if (is_blank_chr(s)) return(as.Date(NA))
  # Numeric string?
  num <- suppressWarnings(as.numeric(s))
  if (!is.na(num)) return(extract_date(num))
  parsed <- suppressWarnings(parse_date_time(s, orders = c("Ymd", "mdy", "dmy"), quiet = TRUE))
  if (is.na(parsed)) return(as.Date(NA))
  as.Date(parsed)
}

# Parse user-typed dates like 2/12/25, 02/12/2025, 2025-02-12
parse_user_date <- function(s) {
  s <- str_trim(as.character(s))
  if (!nzchar(s) || is.na(s)) return(as.Date(NA))
  parsed <- suppressWarnings(parse_date_time(
    s,
    orders = c("Ymd", "mdy", "mdY", "dmy", "dmY", "m/d/y", "m/d/Y"),
    quiet = TRUE
  ))
  if (is.na(parsed)) return(as.Date(NA))
  as.Date(parsed)
}

prompt_with_default <- function(prompt, default) {
  default_txt <- if (is.na(default) || is.null(default)) "" else as.character(default)
  reply <- readline(sprintf("%s [%s]: ", prompt, default_txt))
  if (!nzchar(str_trim(reply))) return(default_txt)
  str_trim(reply)
}

fmt_clock <- function(sec) {
  sec <- round(sec) %% 86400
  sprintf("%02d:%02d:%02d", sec %/% 3600, (sec %% 3600) %/% 60, sec %% 60)
}

# Mode helper for bin detection
statistical_mode <- function(x) {
  x <- x[!is.na(x)]
  if (length(x) == 0) return(NA_real_)
  ux <- unique(x)
  ux[which.max(tabulate(match(x, ux)))]
}

col_to_letter <- function(n) {
  out <- character(length(n))
  for (i in seq_along(n)) {
    x <- n[i]
    s <- ""
    while (x > 0) {
      r <- (x - 1) %% 26
      s <- paste0(LETTERS[r + 1], s)
      x <- (x - 1) %/% 26
    }
    out[i] <- s
  }
  out
}

# =====================================================
# Preprocess Excel → metadata + count table
# =====================================================

if (is.null(input_xlsx) || !nzchar(input_xlsx)) {
  candidates <- list.files(pattern = "\\.xlsx$", full.names = TRUE)
  candidates <- candidates[!grepl(
    "wake_episodes|sample_file|~\\$",
    basename(candidates),
    ignore.case = TRUE
  )]
  if (length(candidates) == 0) stop("No .xlsx file found in working directory.")
  input_xlsx <- candidates[[1]]
}

if (!file.exists(input_xlsx)) {
  stop("Input file not found: ", input_xlsx)
}

message("Reading: ", input_xlsx)
raw <- read_excel(input_xlsx, sheet = sheet, col_names = FALSE, .name_repair = "minimal")

n_rows <- nrow(raw)
n_cols <- ncol(raw)
if (n_rows < 4) stop("File has fewer than 4 rows; expected metadata + data.")

col_date <- 1
col_time <- 2

# Locate metadata relative to the "cage" label in column B.
# readxl may drop a leading blank Excel row, so absolute row numbers are unreliable.
col_b <- vapply(seq_len(n_rows), function(r) {
  s <- cell_chr(raw[[col_time]][r])
  if (is.na(s)) NA_character_ else tolower(s)
}, character(1))

row_cage <- which(col_b == "cage")[1]
if (is.na(row_cage) || row_cage < 2) {
  stop('Could not find "cage" label in column B. Check the Excel layout.')
}
row_animal <- row_cage - 1
row_label  <- row_cage + 1
row_data0  <- row_cage + 2

if (row_data0 > n_rows) {
  stop("No data rows found below the metadata block.")
}

message(
  "Detected rows — animal #: ", row_animal,
  ", cage: ", row_cage,
  ", label: ", row_label,
  ", data start: ", row_data0
)

# Animal columns = those with a non-blank animal # in the animal row, starting at col 3
meta_rows <- list()

for (c in seq(3, n_cols)) {
  animal_raw <- cell_chr(raw[[c]][row_animal])
  if (is_blank_chr(animal_raw)) next

  animal_id <- suppressWarnings(as.integer(animal_raw))
  if (is.na(animal_id)) {
    warning("Skipping column ", c, ": animal # is not an integer (", animal_raw, ").")
    next
  }

  cage_raw <- cell_chr(raw[[c]][row_cage])
  if (!is.na(cage_raw) && tolower(cage_raw) == "cage") cage_raw <- NA_character_

  label_raw <- cell_chr(raw[[c]][row_label])
  if (is_blank_chr(label_raw)) label_raw <- NA_character_
  if (is_blank_chr(cage_raw)) cage_raw <- NA_character_

  meta_rows[[length(meta_rows) + 1]] <- tibble(
    excel_col = c,
    excel_col_letter = col_to_letter(c),
    animal_id = animal_id,
    cage_code = cage_raw,
    animal_label = label_raw,
    column_name = paste0("animal_", animal_id)
  )
}

animal_meta <- bind_rows(meta_rows)

if (nrow(animal_meta) == 0) {
  stop("No animal columns found. Expected integer animal # values in Excel row 2, columns C+.")
}

animal_meta <- animal_meta %>%
  select(excel_col_letter, animal_id, cage_code, animal_label, column_name, excel_col)

# ---- Parse contiguous time block from row 5 ----
# Stop at first blank / summary / unparseable time so extra blocks are excluded.
keep_rows <- integer(0)
keep_secs <- numeric(0)

for (r in seq(row_data0, n_rows)) {
  sec <- to_sec_of_day(raw[[col_time]][r])
  if (is.na(sec)) break
  keep_rows <- c(keep_rows, r)
  keep_secs <- c(keep_secs, sec)
}

if (length(keep_rows) == 0) {
  stop("No parseable time values found in column B from row 5 onward.")
}

# ---- Detect bin size from consecutive positive time gaps ----
sec_diffs <- diff(keep_secs)
# Positive gaps only (ignore midnight wrap, which is negative)
pos_diffs_min <- round(sec_diffs[sec_diffs > 0] / 60)
detected_bin <- statistical_mode(pos_diffs_min)

if (is.null(bin_minutes) || is.na(bin_minutes)) {
  if (is.na(detected_bin) || detected_bin <= 0) {
    stop("Could not auto-detect bin size. Set bin_minutes manually in CONFIG.")
  }
  bin_minutes <- as.integer(detected_bin)
} else {
  bin_minutes <- as.integer(bin_minutes)
}

# ---- Parameter confirmation / customization ----
# Suggested start date from the file (or fallback)
suggested_start_date <- extract_date(raw[[col_date]][row_data0])
if (is.na(suggested_start_date)) {
  suggested_start_date <- start_date_fallback
}
file_start_date <- suggested_start_date

message("\n===== Preprocess summary =====")
message("File: ", basename(input_xlsx))
message("Suggested start date: ", suggested_start_date)
message("Detected bin size: ", bin_minutes, " minutes")
message("Rows (bins) per animal: ", length(keep_rows))
message("First clock time: ", fmt_clock(keep_secs[1]))
message("Last clock time:  ", fmt_clock(keep_secs[length(keep_secs)]))
message("Midnight crossings (clock wrap): ", sum(c(FALSE, keep_secs[-1] < keep_secs[-length(keep_secs)])))
message("\nAnimal metadata:")
print(animal_meta %>% select(excel_col_letter, animal_id, cage_code, animal_label), n = Inf)

if (isTRUE(ask_user_parameters) && interactive()) {
  message("\n===== Customize parameters (press Enter to keep default) =====")

  # Threshold
  thresh_reply <- prompt_with_default(
    "Activity threshold count (comma-separated if multiple)",
    paste(thresholds, collapse = ",")
  )
  thresh_parsed <- suppressWarnings(as.numeric(str_split(thresh_reply, ",")[[1]]))
  thresh_parsed <- thresh_parsed[!is.na(thresh_parsed)]
  if (length(thresh_parsed) == 0) stop("No valid threshold entered.")
  thresholds <- thresh_parsed

  # Min episode length
  min_reply <- prompt_with_default(
    "Minimum episode length in minutes",
    min_episode_minutes
  )
  min_parsed <- suppressWarnings(as.numeric(min_reply))
  if (is.na(min_parsed) || min_parsed < 0) stop("Invalid minimum episode length.")
  min_episode_minutes <- min_parsed

  # Start date
  date_reply <- prompt_with_default(
    "Date data collection began (e.g. 2/12/25 or 2025-02-12)",
    suggested_start_date
  )
  date_parsed <- parse_user_date(date_reply)
  if (is.na(date_parsed)) stop("Could not parse start date: ", date_reply)
  file_start_date <- date_parsed

  # Optional animal_label edits
  message("\nCurrent animal labels:")
  print(animal_meta %>% select(animal_id, cage_code, animal_label), n = Inf)
  edit_reply <- tolower(str_trim(readline("Edit animal labels? (y/n) [n]: ")))
  if (edit_reply %in% c("y", "yes")) {
    message("For each animal: Enter=keep current, '-'=blank, or type a new label.")
    for (i in seq_len(nrow(animal_meta))) {
      cur <- animal_meta$animal_label[i]
      cur_disp <- if (is.na(cur) || !nzchar(cur)) "" else cur
      cage_disp <- if (is.na(animal_meta$cage_code[i])) "NA" else animal_meta$cage_code[i]
      ans <- readline(sprintf(
        "  Animal %s (cage %s) label [%s]: ",
        animal_meta$animal_id[i], cage_disp, cur_disp
      ))
      ans <- str_trim(ans)
      if (!nzchar(ans)) next
      if (ans == "-") {
        animal_meta$animal_label[i] <- NA_character_
      } else {
        animal_meta$animal_label[i] <- ans
      }
    }
    message("Updated animal labels:")
    print(animal_meta %>% select(animal_id, cage_code, animal_label), n = Inf)
  }

  confirm <- tolower(str_trim(readline("Proceed with analysis using these settings? (y/n): ")))
  if (!confirm %in% c("y", "yes")) {
    stop("Aborted by user.")
  }
} else if (isTRUE(ask_user_parameters) && !interactive()) {
  message("Non-interactive session: using CONFIG defaults (no prompts).")
}

message("\n===== Settings for analysis =====")
message("Threshold(s): ", paste(thresholds, collapse = ", "))
message("Min episode length: ", min_episode_minutes, " minutes")
message("Start date: ", file_start_date)
message("Bin size: ", bin_minutes, " minutes")

# ---- Build time_fixed with midnight day rollover ----
# Any backward jump in clock time advances the day by 1.
wrap_flags <- c(FALSE, keep_secs[-1] < keep_secs[-length(keep_secs)])
day_offset_within <- cumsum(wrap_flags)
time_fixed <- as.POSIXct(file_start_date, tz = tz_out) +
  days(day_offset_within) +
  seconds(keep_secs)

# Count matrix for animal columns
count_list <- list(time_fixed = time_fixed)
for (i in seq_len(nrow(animal_meta))) {
  c <- animal_meta$excel_col[i]
  nm <- animal_meta$column_name[i]
  vals <- vapply(keep_rows, function(r) {
    v <- suppressWarnings(as.numeric(cell_chr(raw[[c]][r])))
    if (length(v) == 0) NA_real_ else v
  }, numeric(1))
  count_list[[nm]] <- vals
}

final_full <- as_tibble(count_list)

message("\nFirst 6 rows:")
print(head(final_full, 6))
message("Last 6 rows:")
print(tail(final_full, 6))

if (any(duplicated(final_full$time_fixed))) {
  warning("Duplicated time_fixed values present after preprocessing.")
}

# =====================================================
# Day / Night period labels
# =====================================================

get_period <- function(datetime) {
  clock_min <- hour(datetime) * 60 + minute(datetime)

  day1_start  <- 19 * 60 + 5   # 7:05 PM
  day1_end    <- 19 * 60 + 35  # 7:35 PM
  night_start <- 19 * 60 + 40  # 7:40 PM
  night_end   <- 7 * 60 + 30   # 7:30 AM
  day2_start  <- 7 * 60 + 35   # 7:35 AM
  day2_end    <- 19 * 60       # 7:00 PM

  case_when(
    (clock_min >= day1_start & clock_min <= day1_end) ~ "Day",
    (clock_min >= night_start | clock_min <= night_end) ~ "Night",
    (clock_min >= day2_start & clock_min <= day2_end) ~ "Day",
    TRUE ~ NA_character_
  )
}

final_full <- final_full %>%
  mutate(period = get_period(time_fixed))

message("\nPeriod counts:")
print(table(final_full$period, useNA = "ifany"))

# =====================================================
# Long format + wake episode detection
# =====================================================

df_long <- final_full %>%
  pivot_longer(
    cols = starts_with("animal_"),
    names_to = "animal",
    values_to = "counts"
  ) %>%
  left_join(
    animal_meta %>% select(column_name, animal_id, cage_code, animal_label),
    by = c("animal" = "column_name")
  ) %>%
  arrange(animal_id, time_fixed)

stopifnot(all(c("time_fixed", "animal", "counts", "period") %in% names(df_long)))

message(
  "df_long rows: ", nrow(df_long),
  " (= ", nrow(final_full), " bins × ", nrow(animal_meta), " animals)"
)
message("Rows per animal:")
print(df_long %>% count(animal_id, animal))

detect_episodes <- function(df, threshold, bin_minutes) {
  if (nrow(df) == 0) return(tibble())

  df <- df %>%
    arrange(time_fixed) %>%
    mutate(active = counts >= threshold)

  n <- nrow(df)
  new_run <- c(TRUE, (df$active[-1] != df$active[-n]) | (df$period[-1] != df$period[-n]))
  df$run_id <- cumsum(new_run)

  df %>%
    group_by(run_id) %>%
    summarise(
      animal = first(animal),
      animal_id = first(animal_id),
      cage_code = first(cage_code),
      animal_label = first(animal_label),
      period = first(period),
      start_time = first(time_fixed),
      end_time = last(time_fixed),
      duration_bins = n(),
      duration_minutes = n() * bin_minutes,
      total_counts = sum(counts, na.rm = TRUE),
      active_flag = first(active),
      .groups = "drop"
    ) %>%
    filter(active_flag) %>%
    select(
      animal, animal_id, cage_code, animal_label, period,
      start_time, end_time, duration_bins, duration_minutes, total_counts
    )
}

episodes_all <- list()

for (thresh in thresholds) {
  message("Processing threshold = ", thresh, " (bin = ", bin_minutes, " min)")

  eps_list <- df_long %>%
    group_split(animal) %>%
    lapply(function(subdf) detect_episodes(subdf, threshold = thresh, bin_minutes = bin_minutes))

  eps_df <- bind_rows(eps_list) %>%
    mutate(threshold = thresh)

  episodes_all[[as.character(thresh)]] <- eps_df
}

episodes_all <- bind_rows(episodes_all)

# Apply minimum episode length filter before IDs / summaries / export
n_before <- nrow(episodes_all)
episodes_all <- episodes_all %>%
  filter(duration_minutes >= min_episode_minutes)
message(
  "Min episode length filter: kept ", nrow(episodes_all), " / ", n_before,
  " episodes (duration >= ", min_episode_minutes, " min)"
)

episodes_all <- episodes_all %>%
  arrange(animal_id, period, threshold, start_time) %>%
  group_by(animal_id, period, threshold) %>%
  mutate(episode_id = row_number()) %>%
  ungroup() %>%
  # Output order: by animal, then true chronology (not Day/Night blocks)
  arrange(animal_id, threshold, start_time)

episodes_summary <- episodes_all %>%
  group_by(animal, animal_id, cage_code, animal_label, period, threshold) %>%
  summarise(
    total_wake_episodes = n(),
    total_wake_minutes = sum(duration_minutes, na.rm = TRUE),
    avg_episode_length = mean(duration_minutes, na.rm = TRUE),
    .groups = "drop"
  )

animals <- sort(unique(df_long$animal))
periods <- c("Day", "Night")
animal_lookup <- df_long %>%
  distinct(animal, animal_id, cage_code, animal_label)

episodes_summary <- expand_grid(
  animal = animals,
  period = periods,
  threshold = thresholds
) %>%
  left_join(animal_lookup, by = "animal") %>%
  left_join(
    episodes_summary %>% select(-animal_id, -cage_code, -animal_label),
    by = c("animal", "period", "threshold")
  ) %>%
  mutate(
    total_wake_episodes = replace_na(total_wake_episodes, 0),
    total_wake_minutes = replace_na(total_wake_minutes, 0),
    avg_episode_length = replace_na(avg_episode_length, 0)
  ) %>%
  arrange(animal_id, period, threshold)

overall_mean_lengths <- episodes_summary %>%
  group_by(threshold, period) %>%
  summarise(
    mean_avg_episode_length = mean(avg_episode_length, na.rm = TRUE),
    sd_avg_episode_length   = sd(avg_episode_length, na.rm = TRUE),
    se_avg_episode_length   = sd(avg_episode_length, na.rm = TRUE) / sqrt(n()),
    .groups = "drop"
  ) %>%
  arrange(threshold, period)

write_xlsx(
  x = list(
    animal_metadata = animal_meta %>% select(excel_col_letter, animal_id, cage_code, animal_label),
    episodes = episodes_all,
    summary = episodes_summary,
    overall_means = overall_mean_lengths
  ),
  path = output_xlsx
)

message("Wrote results to: ", output_xlsx)
