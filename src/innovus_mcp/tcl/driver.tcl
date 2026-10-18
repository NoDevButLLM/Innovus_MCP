namespace eval ::innovus_mcp {
    variable root
    variable token
    variable native_id ""
}

proc ::innovus_mcp::frame {kind id} {
    variable token
    # tmux consumes this private OSC without painting it; pipe-pane retains its bytes.
    puts -nonewline stdout "\x1b\]777;IMCP:${token}:${kind}:${id}\x07"
    flush stdout
}

proc ::innovus_mcp::init {directory session_token id} {
    variable root $directory
    variable token $session_token
    ::innovus_mcp::frame READY $id
}

proc ::innovus_mcp::ping {id} {
    ::innovus_mcp::frame READY $id
}

proc ::innovus_mcp::set_native {id} {
    variable native_id $id
    ::innovus_mcp::frame READY $id
}

proc ::innovus_mcp::release_native {} {
    variable native_id
    if {$native_id eq ""} {error "No active native interval"}
    ::innovus_mcp::frame RELEASE $native_id
    set native_id ""
}

proc ::innovus_mcp::write_utf8 {path text} {
    set channel [open $path wb]
    fconfigure $channel -translation binary -encoding binary
    puts -nonewline $channel [encoding convertto utf-8 $text]
    close $channel
}

proc ::innovus_mcp::preview_script {script} {
    set lines [split [string trim $script \n] \n]
    set shown 0
    set truncated 0
    foreach line $lines {
        if {$shown >= 6} {set truncated 1; break}
        set line [string map [list "\t" "    " "\r" ""] $line]
        # Display control characters as text, without changing the executed script.
        regsub -all {[\x00-\x1f\x7f]} $line {?} line
        if {[string length $line] > 76} {
            set line "[string range $line 0 71] ..."
            set truncated 1
        }
        puts "    $line"
        incr shown
    }
    if {$truncated} {puts "    ... (preview shortened; full script in job artifacts)"}
}

proc ::innovus_mcp::run {id} {
    variable root
    set dir [file join $root jobs $id]
    variable job_dir $dir
    set channel [open [file join $dir request.tcl] r]
    fconfigure $channel -encoding utf-8 -translation lf
    set script [read $channel]
    close $channel
    set label [string range $id 0 7]
    puts "\n\[innovus-mcp\] BEGIN job=$label"
    ::innovus_mcp::preview_script $script
    flush stdout
    ::innovus_mcp::frame START $id
    set code [catch {uplevel #0 $script} result options]
    ::innovus_mcp::write_utf8 [file join $dir result.txt] $result
    foreach {option filename} {-errorinfo error_info.txt -errorcode error_code.txt} {
        set value ""
        if {[dict exists $options $option]} {set value [dict get $options $option]}
        ::innovus_mcp::write_utf8 [file join $dir $filename] $value
    }
    set manifest [open [file join $dir completion.meta.tmp] w]
    puts $manifest "version=1\ncode=$code"
    close $manifest
    file rename -force [file join $dir completion.meta.tmp] [file join $dir completion.meta]
    flush stdout
    catch {flush stderr}
    ::innovus_mcp::frame DONE $id
    set status [expr {$code == 0 ? "succeeded" : "failed"}]
    puts "\n\[innovus-mcp\] END job=$label status=$status tcl_code=$code"
    flush stdout
    return ""
}
