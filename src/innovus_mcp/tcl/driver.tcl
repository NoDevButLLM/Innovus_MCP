namespace eval ::innovus_mcp {
    variable root
    variable token
    variable native_id ""
}

proc ::innovus_mcp::frame {kind id} {
    variable token
    puts -nonewline stdout "\x1eIMCP:${token}:${kind}:${id}\x1f"
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

proc ::innovus_mcp::run {id} {
    variable root
    set dir [file join $root jobs $id]
    variable job_dir $dir
    set channel [open [file join $dir request.tcl] r]
    fconfigure $channel -encoding utf-8 -translation lf
    set script [read $channel]
    close $channel
    puts "\n\[innovus-mcp job $id\] [string range [string map {\n { }} $script] 0 255]"
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
    return ""
}
